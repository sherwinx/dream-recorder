#!/usr/bin/env python3
"""Import Plaud recordings into Dream Recorder with a local retry outbox."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests


DEFAULT_OUTBOX_DIR = "db/plaud_outbox"
DEFAULT_PLAUD_CLI = "plaud"
DEFAULT_DAYONE_COMMAND = "/Applications/Day One.app/Contents/MacOS/dayone"
DEFAULT_DAYONE_JOURNAL_NAME = "每日一记"
DEFAULT_TIMEZONE = "America/Los_Angeles"
ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T[^\s]+")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
UNAVAILABLE_TRANSCRIPT_RE = re.compile(
    r"^\s*(transcript\s+(is\s+)?not\s+available|transcript\s+unavailable|unavailable)\b",
    re.IGNORECASE,
)


class PlaudImporterError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def recording_dir(outbox_dir: Path, recording_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", recording_id).strip("._")
    if not safe_id:
        raise PlaudImporterError("recording id produced an empty outbox path")
    return outbox_dir / safe_id


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def clean_plaud_transcript(transcript: str | None) -> str:
    cleaned = (transcript or "").strip()
    if UNAVAILABLE_TRANSCRIPT_RE.match(cleaned):
        return ""
    lines = []
    for line in cleaned.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- Fetching transcript"):
            continue
        if stripped.lower().startswith("transcript:"):
            continue
        stripped = re.sub(
            r"^\[\d{2}:\d{2}(?::\d{2})?\s*-\s*\d{2}:\d{2}(?::\d{2})?\]\s*",
            "",
            stripped,
        )
        stripped = re.sub(r"^Speaker\s+\d+:\s*", "", stripped, flags=re.IGNORECASE)
        if stripped:
            lines.append(stripped)
    return "\n".join(lines)


def mark_status(path: Path, status: str, error: str | None = None) -> None:
    state_path = path / "status.json"
    state = read_json(state_path, default={}) or {}
    state.update({
        "status": status,
        "updated_at": utc_now().isoformat(),
    })
    if error is not None:
        state["last_error"] = error
    elif status in ("queued", "uploaded", "waiting_transcript"):
        state.pop("last_error", None)
    write_json(state_path, state)


def mark_dayone_status(
    path: Path,
    status: str,
    error: str | None = None,
    *,
    entry_id: str | None = None,
    action: str | None = None,
) -> None:
    state_path = path / "status.json"
    state = read_json(state_path, default={}) or {}
    state.update({
        "dayone_status": status,
        "dayone_updated_at": utc_now().isoformat(),
    })
    if entry_id:
        state["dayone_entry_id"] = entry_id
    if action:
        state["dayone_action"] = action
    if error is not None:
        state["dayone_last_error"] = error
    elif status == "synced":
        state.pop("dayone_last_error", None)
    write_json(state_path, state)


def local_datetime_for_payload(payload: dict[str, Any], timezone_name: str) -> datetime:
    local_tz = ZoneInfo(timezone_name)
    started_at = payload.get("start_at") or payload.get("created_at")
    parsed = parse_iso(started_at)
    if parsed is None:
        parsed = utc_now()
    return parsed.astimezone(local_tz)


@dataclass
class PlaudCliClient:
    command: str = DEFAULT_PLAUD_CLI
    timeout_seconds: int = 60

    def run(self, *args: str) -> str:
        result = subprocess.run(
            [self.command, *args],
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if result.returncode != 0:
            raise PlaudImporterError(result.stderr.strip() or result.stdout.strip())
        return result.stdout.strip()

    def list_files(self) -> list[dict[str, Any]]:
        output = self.run("files")
        parsed = self._parse_json(output)
        if isinstance(parsed, dict):
            for key in ("files", "data", "results"):
                if isinstance(parsed.get(key), list):
                    return parsed[key]
        if isinstance(parsed, list):
            return parsed
        return self._parse_files_table(output)

    def file(self, recording_id: str) -> dict[str, Any]:
        output = self.run("file", recording_id)
        parsed = self._parse_json(output)
        if isinstance(parsed, dict):
            return parsed.get("file") or parsed.get("data") or parsed
        return self._parse_key_values(output)

    def audio_url(self, recording_id: str) -> str:
        output = self.run("audio", recording_id)
        for token in output.split():
            if token.startswith("http://") or token.startswith("https://"):
                return token
        raise PlaudImporterError(f"Plaud audio URL was not found for {recording_id}")

    def transcript(self, recording_id: str) -> str:
        return self.run("transcript", recording_id)

    def _parse_json(self, output: str) -> Any:
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return None

    def _parse_key_values(self, output: str) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for line in output.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip().lower().replace(" ", "_")
            values[key] = value.strip()
        return values

    def _parse_files_table(self, output: str) -> list[dict[str, Any]]:
        if re.search(r"Files on this page:\s*0", output):
            return []

        files = []
        for line in output.splitlines():
            if not line.strip() or line.lower().startswith(("id ", "id\t", "---")):
                continue
            columns = re.split(r"\s{2,}|\t+", line.strip())
            if len(columns) >= 3 and DATE_RE.fullmatch(columns[-2]):
                recording_id = columns[0].strip()
                name = columns[1].strip()
                files.append({"id": recording_id, "name": name, "created_at": columns[-2].strip()})
                continue

            match = ISO_RE.search(line)
            if match:
                prefix = line[:match.start()].strip()
                pieces = re.split(r"\s{2,}|\t+", prefix, maxsplit=1)
                if not pieces:
                    continue
                recording_id = pieces[0].strip()
                name = pieces[1].strip() if len(pieces) > 1 else ""
                files.append({"id": recording_id, "name": name, "created_at": match.group(0)})
        if not files:
            raise PlaudImporterError(
                "Could not parse `plaud files` output. If Plaud adds JSON output, update this importer to use it."
            )
        return files


def download_audio(url: str, destination: Path, timeout_seconds: int = 120) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=timeout_seconds) as response:
        response.raise_for_status()
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)


def queue_recording(
    *,
    outbox_dir: Path,
    metadata: dict[str, Any],
    audio_path: Path,
    transcript: str,
) -> Path:
    recording_id = metadata["id"]
    cleaned_transcript = clean_plaud_transcript(transcript)
    target_dir = recording_dir(outbox_dir, recording_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_audio = target_dir / audio_path.name
    if audio_path.resolve() != target_audio.resolve():
        shutil.copy2(audio_path, target_audio)
    write_json(target_dir / "metadata.json", metadata)
    (target_dir / "transcript.txt").write_text(cleaned_transcript, encoding="utf-8")
    write_json(target_dir / "payload.json", {
        "plaud_recording_id": recording_id,
        "title": metadata.get("name") or metadata.get("title") or "",
        "start_at": metadata.get("start_at") or metadata.get("created_at"),
        "audio_filename": target_audio.name,
        "metadata_json": metadata,
    })
    mark_status(target_dir, "queued" if cleaned_transcript else "waiting_transcript")
    return target_dir


def is_locally_complete(target_dir: Path) -> bool:
    """True when the outbox already holds this recording's audio and a usable
    transcript, so re-fetching it from Plaud would download the same bytes again.

    Recordings still waiting on Plaud's asynchronous transcription deliberately
    return False -- picking those up later is why we re-pull at all.
    """
    payload = read_json(target_dir / "payload.json")
    if not payload:
        return False
    audio_filename = payload.get("audio_filename")
    if not audio_filename or not (target_dir / audio_filename).exists():
        return False
    transcript_path = target_dir / "transcript.txt"
    if not transcript_path.exists():
        return False
    return bool(clean_plaud_transcript(transcript_path.read_text(encoding="utf-8")))


def pull_recent_recordings(
    *,
    outbox_dir: Path,
    cli: PlaudCliClient,
    lookback_days: int,
    recording_id: str | None = None,
) -> dict[str, Any]:
    cutoff = utc_now() - timedelta(days=lookback_days)
    pulled = 0
    skipped = 0
    waiting_transcript = 0
    for item in cli.list_files():
        item_recording_id = item.get("id")
        if not item_recording_id:
            continue
        if recording_id and item_recording_id != recording_id:
            skipped += 1
            continue
        target_dir = recording_dir(outbox_dir, item_recording_id)
        status = read_json(target_dir / "status.json", default={}) or {}
        if status.get("status") == "ignored":
            skipped += 1
            continue
        if is_locally_complete(target_dir):
            # What we already hold locally -- not the upload status -- decides
            # whether there is anything left to fetch. "uploaded" only means the
            # Pi got it, so gating on it stranded recordings that Plaud
            # transcribed after the upload; and with the Pi offline nothing ever
            # reaches "uploaded", which re-downloaded everything on every run.
            skipped += 1
            continue

        details = {**item, **cli.file(item_recording_id)}
        started_at = details.get("start_at") or details.get("created_at")
        started_dt = parse_iso(started_at)
        if started_dt and started_dt < cutoff:
            skipped += 1
            continue

        audio_url = cli.audio_url(item_recording_id)
        audio_path = target_dir / f"{item_recording_id}.mp3"
        download_audio(audio_url, audio_path)
        transcript = cli.transcript(item_recording_id)
        queue_recording(
            outbox_dir=outbox_dir,
            metadata=details,
            audio_path=audio_path,
            transcript=transcript,
        )
        if not clean_plaud_transcript(transcript):
            waiting_transcript += 1
        pulled += 1
    return {"pulled": pulled, "skipped": skipped, "waiting_transcript": waiting_transcript}


def upload_pending(
    *,
    outbox_dir: Path,
    pi_import_url: str,
    token: str | None = None,
    recording_id: str | None = None,
    timeout_seconds: int = 900,
    connect_timeout_seconds: int = 10,
) -> dict[str, Any]:
    uploaded = 0
    failed = 0
    waiting_transcript = 0
    for path in sorted(outbox_dir.iterdir() if outbox_dir.exists() else []):
        if not path.is_dir():
            continue
        if recording_id and path.name != recording_id:
            continue
        status = read_json(path / "status.json", default={}) or {}
        if status.get("status") in ("uploaded", "ignored"):
            continue
        payload = read_json(path / "payload.json")
        if not payload:
            continue
        transcript_path = path / "transcript.txt"
        audio_path = path / payload["audio_filename"]
        if not audio_path.exists():
            mark_status(path, "failed", f"Missing audio file: {audio_path}")
            failed += 1
            continue

        transcript_text = clean_plaud_transcript(
            transcript_path.read_text(encoding="utf-8") if transcript_path.exists() else ""
        )
        if not transcript_text:
            mark_status(path, "waiting_transcript")
            waiting_transcript += 1
            continue

        data = {
            "plaud_recording_id": payload["plaud_recording_id"],
            "title": payload.get("title") or "",
            "start_at": payload.get("start_at") or "",
            "audio_filename": payload["audio_filename"],
            "transcript": transcript_text,
            "metadata_json": json.dumps(payload.get("metadata_json") or {}, ensure_ascii=False),
        }
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            with audio_path.open("rb") as audio_handle:
                response = requests.post(
                    pi_import_url,
                    data=data,
                    files={"audio_file": (audio_path.name, audio_handle)},
                    headers=headers,
                    # Separate connect from read: an offline Pi drops packets
                    # rather than refusing, so a single long timeout would stall
                    # the daily run for hours across every queued recording.
                    timeout=(connect_timeout_seconds, timeout_seconds),
                )
            response.raise_for_status()
            mark_status(path, "uploaded")
            uploaded += 1
        except Exception as exc:
            mark_status(path, "failed", str(exc))
            failed += 1
    return {"uploaded": uploaded, "failed": failed, "waiting_transcript": waiting_transcript}


def sync_dayone_pending(
    *,
    outbox_dir: Path,
    journal_name: str = DEFAULT_DAYONE_JOURNAL_NAME,
    dayone_command: str = DEFAULT_DAYONE_COMMAND,
    timezone_name: str = DEFAULT_TIMEZONE,
    recording_id: str | None = None,
    upsert_func=None,
) -> dict[str, Any]:
    synced = 0
    failed = 0
    waiting_transcript = 0
    skipped = 0
    if upsert_func is None:
        from scripts.dayone_worker import upsert_daily_reflection_dream

        upsert_func = upsert_daily_reflection_dream

    for path in sorted(outbox_dir.iterdir() if outbox_dir.exists() else []):
        if not path.is_dir():
            continue
        if recording_id and path.name != recording_id:
            continue
        status = read_json(path / "status.json", default={}) or {}
        if status.get("status") == "ignored" or status.get("dayone_status") == "synced":
            skipped += 1
            continue
        payload = read_json(path / "payload.json")
        if not payload:
            skipped += 1
            continue

        transcript_path = path / "transcript.txt"
        transcript_text = clean_plaud_transcript(
            transcript_path.read_text(encoding="utf-8") if transcript_path.exists() else ""
        )
        if not transcript_text:
            mark_dayone_status(path, "waiting_transcript")
            waiting_transcript += 1
            continue

        local_dt = local_datetime_for_payload(payload, timezone_name)
        try:
            result = upsert_func(
                dream_text=transcript_text,
                journal_name=journal_name,
                command=dayone_command,
                target_date=local_dt.date(),
                dream_local_time=local_dt.strftime("%H:%M"),
                idempotency_key=f"plaud:{payload['plaud_recording_id']}",
            )
            entry = result.get("entry") or {}
            mark_dayone_status(
                path,
                "synced",
                entry_id=entry.get("entryId") or entry.get("id"),
                action=result.get("action"),
            )
            synced += 1
        except Exception as exc:
            mark_dayone_status(path, "failed", str(exc))
            failed += 1
    return {
        "synced": synced,
        "failed": failed,
        "waiting_transcript": waiting_transcript,
        "skipped": skipped,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["pull", "upload-pending", "sync-dayone", "sync"])
    parser.add_argument("--outbox-dir", default=os.getenv("PLAUD_IMPORT_OUTBOX_DIR", DEFAULT_OUTBOX_DIR))
    parser.add_argument("--plaud-cli", default=os.getenv("PLAUD_CLI_PATH", DEFAULT_PLAUD_CLI))
    parser.add_argument("--pi-import-url", default=os.getenv("PLAUD_PI_IMPORT_URL"))
    parser.add_argument("--token", default=os.getenv("PLAUD_IMPORT_TOKEN"))
    parser.add_argument(
        "--dayone-journal-name",
        default=os.getenv("DAYONE_JOURNAL_NAME", DEFAULT_DAYONE_JOURNAL_NAME),
    )
    parser.add_argument(
        "--dayone-command",
        default=os.getenv("DAYONE_COMMAND", DEFAULT_DAYONE_COMMAND),
    )
    parser.add_argument(
        "--timezone",
        default=os.getenv("DREAM_RECORDER_TIMEZONE", DEFAULT_TIMEZONE),
    )
    parser.add_argument(
        "--skip-dayone",
        action="store_true",
        help="Do not write queued Plaud transcripts directly to Day One during sync.",
    )
    parser.add_argument(
        "--skip-pi-upload",
        action="store_true",
        help="Do not upload queued Plaud recordings to the Dream Recorder Pi during sync.",
    )
    parser.add_argument(
        "--pi-optional",
        action="store_true",
        help=(
            "Treat Pi upload failures as best-effort: still attempt and log them, "
            "but do not fail the run. Use when the Pi is not always powered on."
        ),
    )
    parser.add_argument("--lookback-days", type=int, default=int(os.getenv("PLAUD_IMPORT_LOOKBACK_DAYS", "14")))
    parser.add_argument("--recording-id", help="Only upload a specific queued Plaud recording id.")
    parser.add_argument("--interval-seconds", type=int, default=0)
    return parser.parse_args(argv)


def step_errors(summary: dict[str, Any]) -> dict[str, str]:
    return {
        name: result["error"]
        for name, result in summary.items()
        if isinstance(result, dict) and "error" in result
    }


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    outbox_dir = Path(args.outbox_dir)
    outbox_dir.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {}

    def step(name: str, func) -> None:
        """Record a step's failure instead of aborting the rest of the run.

        The three steps are independent, so one broken dependency must not stall
        the others: a missing Plaud CLI or an offline Pi should still leave
        transcripts already in the outbox free to reach Day One.
        """
        try:
            summary[name] = func()
        except Exception as exc:
            summary[name] = {"error": str(exc)}

    if args.command in ("pull", "sync"):
        step(
            "pull",
            lambda: pull_recent_recordings(
                outbox_dir=outbox_dir,
                cli=PlaudCliClient(command=args.plaud_cli),
                lookback_days=args.lookback_days,
                recording_id=args.recording_id,
            ),
        )
    if args.command in ("sync-dayone", "sync") and not args.skip_dayone:
        step(
            "dayone",
            lambda: sync_dayone_pending(
                outbox_dir=outbox_dir,
                journal_name=args.dayone_journal_name,
                dayone_command=args.dayone_command,
                timezone_name=args.timezone,
                recording_id=args.recording_id,
            ),
        )
    if args.command in ("upload-pending", "sync") and not args.skip_pi_upload:

        def upload() -> dict[str, Any]:
            if not args.pi_import_url:
                raise PlaudImporterError("--pi-import-url or PLAUD_PI_IMPORT_URL is required")
            return upload_pending(
                outbox_dir=outbox_dir,
                pi_import_url=args.pi_import_url,
                token=args.token,
                recording_id=args.recording_id,
            )

        step("upload", upload)
    return summary


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    while True:
        summary = run_once(args)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        # Isolating step failures must not make them silent: launchd only
        # surfaces a run as failed via a nonzero exit code. Best-effort steps
        # are still reported but excluded from that code, so a routinely
        # offline Pi does not desensitise us to real failures.
        errors = step_errors(summary)
        best_effort = {"upload"} if args.pi_optional else set()
        for name, message in errors.items():
            label = "warning" if name in best_effort else "failed"
            print(f"plaud importer step {name!r} {label}: {message}", file=sys.stderr)
        if args.interval_seconds <= 0:
            return 1 if errors.keys() - best_effort else 0
        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:
        print(f"plaud importer failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
