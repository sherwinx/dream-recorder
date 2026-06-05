import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import ffmpeg

from functions.audio import generate_video_prompt, transcribe_audio
from functions.config_loader import get_config
from functions.dayone_sync import submit_pending_dayone_sync_jobs
from functions.dream_db import DreamData
from functions.video import generate_video


class PlaudImportError(RuntimeError):
    pass


UNAVAILABLE_TRANSCRIPT_RE = re.compile(
    r'^\s*(transcript\s+(is\s+)?not\s+available|transcript\s+unavailable|unavailable)\b',
    re.IGNORECASE,
)


def clean_plaud_transcript(transcript):
    cleaned = (transcript or '').strip()
    if UNAVAILABLE_TRANSCRIPT_RE.match(cleaned):
        return ''
    lines = []
    for line in cleaned.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('- Fetching transcript'):
            continue
        if stripped.lower().startswith('transcript:'):
            continue
        stripped = re.sub(
            r'^\[\d{2}:\d{2}(?::\d{2})?\s*-\s*\d{2}:\d{2}(?::\d{2})?\]\s*',
            '',
            stripped,
        )
        stripped = re.sub(r'^Speaker\s+\d+:\s*', '', stripped, flags=re.IGNORECASE)
        if stripped:
            lines.append(stripped)
    return '\n'.join(lines)


def parse_plaud_datetime(value, config=None):
    config = config or get_config()
    local_tz = ZoneInfo(config.get('DEFAULT_TIMEZONE') or 'America/Los_Angeles')
    if not value:
        return datetime.now(local_tz)
    normalized = value.strip()
    if normalized.endswith('Z'):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise PlaudImportError(f"Invalid Plaud start_at value: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(local_tz)


def safe_plaud_filename(plaud_recording_id, original_filename=None):
    ext = Path(original_filename or '').suffix.lower()
    if not ext:
        ext = '.mp3'
    safe_id = re.sub(r'[^A-Za-z0-9_.-]+', '_', plaud_recording_id).strip('._')
    if not safe_id:
        raise PlaudImportError("Plaud recording id produced an empty filename")
    return f"plaud/{safe_id}{ext}"


def save_plaud_audio(uploaded_file, plaud_recording_id, original_filename=None, config=None):
    config = config or get_config()
    filename = safe_plaud_filename(
        plaud_recording_id,
        original_filename=original_filename or getattr(uploaded_file, 'filename', None),
    )
    audio_dir = Path(config['RECORDINGS_DIR'])
    destination = audio_dir / filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    uploaded_file.save(destination)
    return filename


def convert_audio_to_wav(source_path):
    suffix = Path(source_path).suffix.lower()
    if suffix == '.wav':
        return source_path, False

    temp_wav = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
    temp_wav_path = temp_wav.name
    temp_wav.close()

    stream = ffmpeg.input(str(source_path))
    stream = ffmpeg.output(stream, temp_wav_path, acodec='pcm_s16le', ac=1, ar=44100)
    ffmpeg.run(stream, overwrite_output=True, quiet=True)
    return temp_wav_path, True


def transcript_for_import(audio_path, transcript=None, logger=None, config=None):
    config = config or get_config()
    cleaned = clean_plaud_transcript(transcript)
    if cleaned:
        return cleaned, 'plaud'
    if str(config.get('PLAUD_REQUIRE_TRANSCRIPT', True)).lower() in ('1', 'true', 'yes'):
        raise PlaudImportError("Plaud transcript is required before import")

    wav_path, should_delete = convert_audio_to_wav(audio_path)
    try:
        generated = transcribe_audio(wav_path, logger=logger, config=config)
        if not generated:
            raise PlaudImportError("Plaud transcript was empty and fallback transcription produced no text")
        return generated, 'google_speech'
    finally:
        if should_delete:
            try:
                os.unlink(wav_path)
            except Exception:
                pass


def process_plaud_import(
    *,
    dream_db,
    plaud_recording_id,
    audio_filename,
    transcript=None,
    start_at=None,
    title=None,
    metadata=None,
    logger=None,
    config=None,
):
    config = config or get_config()
    if not plaud_recording_id:
        raise PlaudImportError("plaud_recording_id is required")
    if not audio_filename:
        raise PlaudImportError("audio_filename is required")

    existing = dream_db.get_plaud_import(plaud_recording_id)
    if existing and existing.get('import_status') == 'completed':
        return {'status': 'duplicate', 'plaud_import': existing}

    dream_db.create_or_update_plaud_import(
        plaud_recording_id,
        title=title,
        started_at=start_at,
        audio_filename=audio_filename,
        import_status='processing',
        raw_metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
        last_error=None,
    )

    try:
        recorded_at = parse_plaud_datetime(start_at, config=config)
        audio_path = Path(config['RECORDINGS_DIR']) / audio_filename
        transcript_text = None
        transcript_source = None
        transcript_record = None

        current = dream_db.get_plaud_import(plaud_recording_id)
        if current and current.get('transcript_id'):
            transcript_id = current['transcript_id']
            transcript_source = current.get('transcript_source') or 'plaud'
        else:
            transcript_text, transcript_source = transcript_for_import(
                audio_path,
                transcript=transcript,
                logger=logger,
                config=config,
            )
            transcript_record = dream_db.save_dream_transcript(
                transcript_text,
                audio_filename=audio_filename,
                recorded_at=recorded_at,
                idempotency_key=f"plaud:{plaud_recording_id}",
            )
            transcript_id = transcript_record['transcript_id']
            dream_db.create_or_update_plaud_import(
                plaud_recording_id,
                transcript_id=transcript_id,
                transcript_source=transcript_source,
            )
            try:
                submit_pending_dayone_sync_jobs(dream_db, config=config, logger=logger)
            except Exception as exc:
                if logger:
                    logger.error(f"Error submitting Plaud Day One job: {exc}")

        if transcript_text is None:
            # Retry path after transcript was already saved.
            transcript_row = dream_db.get_dream_transcript(transcript_id)
            if transcript_row:
                transcript_text = transcript_row['transcript']
            else:
                raise PlaudImportError("Existing Plaud transcript could not be loaded for retry")

        luma_extend = str(config['LUMA_EXTEND']).lower() in ('1', 'true', 'yes')
        video_prompt = generate_video_prompt(
            transcription=transcript_text,
            luma_extend=luma_extend,
            logger=logger,
            config=config,
        )
        if not video_prompt:
            raise PlaudImportError("Failed to generate video prompt")

        video_filename, thumb_filename = generate_video(
            prompt=video_prompt,
            luma_extend=luma_extend,
            logger=logger,
        )
        dream_id = dream_db.save_dream(DreamData(
            user_prompt=transcript_text,
            generated_prompt=video_prompt,
            audio_filename=audio_filename,
            video_filename=video_filename,
            thumb_filename=thumb_filename,
            status='completed',
        ).model_dump())
        dream_db.link_transcript_to_dream(transcript_id, dream_id)
        plaud_import = dream_db.complete_plaud_import(
            plaud_recording_id,
            transcript_id=transcript_id,
            dream_id=dream_id,
            audio_filename=audio_filename,
            transcript_source=transcript_source,
        )
        return {'status': 'imported', 'dream_id': dream_id, 'plaud_import': plaud_import}
    except Exception as exc:
        dream_db.mark_plaud_import_status(plaud_recording_id, 'failed', error=exc)
        raise
