import math
import os
import re
import tempfile
import wave

import ffmpeg

from datetime import datetime
from functions.video import generate_video
from functions.config_loader import get_config


def create_wav_file(audio_buffer):
    """Create a new WAV file in the audio buffer with the correct format."""
    wav_file = wave.open(audio_buffer, 'wb')
    wav_file.setnchannels(int(get_config()['AUDIO_CHANNELS']))
    wav_file.setsampwidth(int(get_config()['AUDIO_SAMPLE_WIDTH']))
    wav_file.setframerate(int(get_config()['AUDIO_FRAME_RATE']))
    return wav_file


def _convert_webm_to_wav(audio_data, filepath, logger=None):
    with tempfile.NamedTemporaryFile(suffix='.webm', delete=False) as temp_webm:
        temp_webm.write(audio_data)
        temp_webm_path = temp_webm.name

    try:
        stream = ffmpeg.input(temp_webm_path)
        stream = ffmpeg.output(stream, filepath, acodec='pcm_s16le', ac=1, ar=44100)
        ffmpeg.run(stream, overwrite_output=True, quiet=True)
        if logger:
            logger.info(f"Saved WAV file to {filepath}")
    finally:
        try:
            os.unlink(temp_webm_path)
        except Exception:
            pass


def save_wav_file(audio_data, filename=None, logger=None):
    """Save the recording locally as mono PCM WAV after converting from WebM."""
    if filename is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"recording_{timestamp}.wav"

    os.makedirs(get_config()['RECORDINGS_DIR'], exist_ok=True)
    filepath = os.path.join(get_config()['RECORDINGS_DIR'], filename)
    _convert_webm_to_wav(audio_data, filepath, logger=logger)
    return filename


def get_wav_duration_seconds(wav_path):
    with wave.open(wav_path, 'rb') as wav_file:
        frames = wav_file.getnframes()
        frame_rate = wav_file.getframerate()
        if frame_rate <= 0:
            return 0
        return frames / float(frame_rate)


def split_wav_file(wav_path, chunk_seconds, logger=None):
    duration = get_wav_duration_seconds(wav_path)
    if duration <= chunk_seconds:
        return [wav_path]

    segment_paths = []
    segment_count = int(math.ceil(duration / chunk_seconds))
    for index in range(segment_count):
        start = index * chunk_seconds
        remaining = max(duration - start, 0)
        segment_duration = min(chunk_seconds, remaining)
        if segment_duration <= 0:
            continue

        segment_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        segment_path = segment_file.name
        segment_file.close()

        stream = ffmpeg.input(wav_path, ss=start, t=segment_duration)
        stream = ffmpeg.output(stream, segment_path, acodec='pcm_s16le', ac=1, ar=44100)
        ffmpeg.run(stream, overwrite_output=True, quiet=True)
        segment_paths.append(segment_path)

    if logger:
        logger.info(f"Split {wav_path} into {len(segment_paths)} chunks")

    return segment_paths


def _get_google_speech_modules():
    from google.api_core.client_options import ClientOptions
    from google.cloud.speech_v2 import SpeechClient
    from google.cloud.speech_v2.types import cloud_speech

    return ClientOptions, SpeechClient, cloud_speech


def _extract_transcript_from_response(response):
    transcripts = []
    for result in getattr(response, 'results', []):
        alternatives = getattr(result, 'alternatives', [])
        if alternatives:
            transcript = getattr(alternatives[0], 'transcript', '')
            if transcript:
                transcripts.append(transcript.strip())
    return ' '.join(transcripts).strip()


def transcribe_wav_chunk_google(wav_path, config=None):
    config = config or get_config()
    project_id = config.get('GOOGLE_CLOUD_PROJECT')
    if not project_id:
        raise ValueError("GOOGLE_CLOUD_PROJECT is required for Google Speech transcription")

    region = config.get('GOOGLE_SPEECH_REGION', 'us')
    model = config.get('GOOGLE_SPEECH_MODEL', 'chirp_3')
    language_codes = config.get('GOOGLE_SPEECH_LANGUAGE_CODES', ['auto'])

    ClientOptions, SpeechClient, cloud_speech = _get_google_speech_modules()
    client = SpeechClient(
        client_options=ClientOptions(api_endpoint=f"{region}-speech.googleapis.com")
    )

    with open(wav_path, 'rb') as audio_file:
        audio_content = audio_file.read()

    recognition_config = cloud_speech.RecognitionConfig(
        auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
        language_codes=language_codes,
        model=model,
    )
    request = cloud_speech.RecognizeRequest(
        recognizer=f"projects/{project_id}/locations/{region}/recognizers/_",
        config=recognition_config,
        content=audio_content,
    )
    response = client.recognize(request=request)
    return _extract_transcript_from_response(response)


def transcribe_audio(wav_path, logger=None, config=None):
    """Transcribe a WAV file with Google Speech, splitting long/local files first."""
    config = config or get_config()
    provider = config.get('TRANSCRIPTION_PROVIDER', 'google_speech')
    if provider != 'google_speech':
        raise ValueError(f"Unsupported transcription provider: {provider}")

    chunk_seconds = float(config.get('GOOGLE_SPEECH_CHUNK_SECONDS', 55))
    max_inline_mb = float(config.get('GOOGLE_SPEECH_MAX_INLINE_MB', 9))
    max_inline_bytes = max_inline_mb * 1024 * 1024

    duration = get_wav_duration_seconds(wav_path)
    file_size = os.path.getsize(wav_path)
    should_split = duration > chunk_seconds or file_size > max_inline_bytes

    if should_split:
        chunk_paths = split_wav_file(wav_path, chunk_seconds, logger=logger)
    else:
        chunk_paths = [wav_path]

    transcripts = []
    try:
        for chunk_path in chunk_paths:
            transcript = transcribe_wav_chunk_google(chunk_path, config=config)
            if transcript:
                transcripts.append(transcript)
    finally:
        for chunk_path in chunk_paths:
            if chunk_path == wav_path:
                continue
            try:
                os.unlink(chunk_path)
            except Exception:
                pass

    return ' '.join(transcripts).strip()


def _get_genai_modules():
    from google import genai
    from google.genai import types

    return genai, types


def normalize_transcription_for_prompt(transcription):
    """Clean up spaced CJK transcripts before sending them to prompt generation."""
    text = transcription.strip()
    text = re.sub(r'(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])', '', text)
    text = re.sub(r'\s+([。！？；，、])', r'\1', text)
    return text


def generate_video_prompt(transcription, luma_extend=False, logger=None, config=None):
    """Generate an enhanced Luma prompt from the transcription using Gemini."""
    config = config or get_config()
    try:
        api_key = config.get('GEMINI_API_KEY')
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required for Gemini prompt generation")

        genai, types = _get_genai_modules()
        system_prompt = (
            config['GPT_SYSTEM_PROMPT_EXTEND']
            if luma_extend
            else config['GPT_SYSTEM_PROMPT']
        )
        client = genai.Client(api_key=api_key)
        thinking_budget = int(config.get('GEMINI_THINKING_BUDGET', -1))
        response = client.models.generate_content(
            model=config.get('GEMINI_PROMPT_MODEL', 'gemini-2.5-flash'),
            contents=[normalize_transcription_for_prompt(transcription)],
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=float(config['GPT_TEMPERATURE']),
                thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget),
            ),
        )
        return response.text.strip() if response.text else None
    except Exception as e:
        if logger:
            logger.error(f"Error generating video prompt: {str(e)}")
        return None


def _emit(socketio, sid, event, payload):
    if sid:
        socketio.emit(event, payload, room=sid)
    else:
        socketio.emit(event, payload)


def process_audio(sid, socketio, dream_db, recording_state, audio_chunks, logger=None):
    """Process recorded audio, generate video, update state, and emit events."""
    try:
        audio_data = b''.join(audio_chunks)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        wav_filename = f"recording_{timestamp}.wav"
        wav_filename = save_wav_file(audio_data, wav_filename, logger)
        wav_path = os.path.join(get_config()['RECORDINGS_DIR'], wav_filename)

        transcription_text = transcribe_audio(wav_path, logger=logger, config=get_config())
        if not transcription_text:
            raise Exception("Failed to transcribe audio")

        recording_state['transcription'] = transcription_text
        _emit(socketio, sid, 'transcription_update', {'text': transcription_text})

        luma_extend = str(get_config()['LUMA_EXTEND']).lower() in ('1', 'true', 'yes')
        video_prompt = generate_video_prompt(
            transcription=transcription_text,
            luma_extend=luma_extend,
            logger=logger,
            config=get_config(),
        )
        if not video_prompt:
            raise Exception("Failed to generate video prompt")

        recording_state['video_prompt'] = video_prompt
        _emit(socketio, sid, 'video_prompt_update', {'text': video_prompt})

        video_filename, thumb_filename = generate_video(
            prompt=video_prompt,
            luma_extend=luma_extend,
            logger=logger,
        )

        from functions.dream_db import DreamData

        dream_data = DreamData(
            user_prompt=recording_state['transcription'],
            generated_prompt=recording_state['video_prompt'],
            audio_filename=wav_filename,
            video_filename=video_filename,
            thumb_filename=thumb_filename,
            status='completed',
        )
        dream_db.save_dream(dream_data.model_dump())

        recording_state['status'] = 'complete'
        recording_state['video_url'] = f"/media/video/{video_filename}"
        _emit(socketio, sid, 'video_ready', {'url': recording_state['video_url']})

        if logger:
            logger.info(f"Audio processed and video generated for SID: {sid}")
    except Exception as e:
        recording_state['status'] = 'error'
        socketio.emit('error', {'message': str(e)})
        if logger:
            logger.error(f"Error processing audio: {str(e)}")
    finally:
        audio_chunks = []
