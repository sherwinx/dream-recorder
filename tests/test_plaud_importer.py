from pathlib import Path

import pytest

from scripts import plaud_importer


def test_queue_and_upload_pending_retries_when_pi_is_offline(tmp_path, monkeypatch):
    audio = tmp_path / "source.mp3"
    audio.write_bytes(b"audio")
    outbox = tmp_path / "outbox"
    plaud_importer.queue_recording(
        outbox_dir=outbox,
        metadata={
            "id": "plaud-abc",
            "name": "Morning dream",
            "start_at": "2026-06-04T06:12:00-07:00",
        },
        audio_path=audio,
        transcript="I dreamed about a train.",
    )

    def failing_post(*_args, **_kwargs):
        raise RuntimeError("pi offline")

    monkeypatch.setattr(plaud_importer.requests, "post", failing_post)
    result = plaud_importer.upload_pending(
        outbox_dir=outbox,
        pi_import_url="http://dreamer.local:5000/api/import/plaud",
    )
    assert result == {"uploaded": 0, "failed": 1, "waiting_transcript": 0}
    status = plaud_importer.read_json(outbox / "plaud-abc" / "status.json")
    assert status["status"] == "failed"
    assert "pi offline" in status["last_error"]

    class FakeResponse:
        def raise_for_status(self):
            return None

    calls = []

    def successful_post(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse()

    monkeypatch.setattr(plaud_importer.requests, "post", successful_post)
    result = plaud_importer.upload_pending(
        outbox_dir=outbox,
        pi_import_url="http://dreamer.local:5000/api/import/plaud",
        token="secret",
    )
    assert result == {"uploaded": 1, "failed": 0, "waiting_transcript": 0}
    status = plaud_importer.read_json(outbox / "plaud-abc" / "status.json")
    assert status["status"] == "uploaded"
    assert calls[0][1]["headers"] == {"Authorization": "Bearer secret"}


def test_upload_pending_can_filter_recording_id(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    for recording_id in ("a", "b"):
        audio = tmp_path / f"{recording_id}.mp3"
        audio.write_bytes(b"audio")
        plaud_importer.queue_recording(
            outbox_dir=outbox,
            metadata={"id": recording_id, "start_at": "2026-06-04T06:12:00-07:00"},
            audio_path=audio,
            transcript=f"dream {recording_id}",
        )

    class FakeResponse:
        def raise_for_status(self):
            return None

    posted_ids = []

    def successful_post(*_args, **kwargs):
        posted_ids.append(kwargs["data"]["plaud_recording_id"])
        return FakeResponse()

    monkeypatch.setattr(plaud_importer.requests, "post", successful_post)
    result = plaud_importer.upload_pending(
        outbox_dir=outbox,
        pi_import_url="http://dreamer.local:5000/api/import/plaud",
        recording_id="b",
    )
    assert result == {"uploaded": 1, "failed": 0, "waiting_transcript": 0}
    assert posted_ids == ["b"]


def test_pull_recent_recordings_can_filter_recording_id(tmp_path, monkeypatch):
    class FakeCli:
        def list_files(self):
            return [
                {"id": "a", "name": "Skip", "created_at": "2026-06-04T06:12:00+00:00"},
                {"id": "b", "name": "Import", "created_at": "2026-06-04T06:13:00+00:00"},
            ]

        def file(self, recording_id):
            return {"id": recording_id, "start_at": "2026-06-04T06:13:00+00:00"}

        def audio_url(self, recording_id):
            return f"https://example.test/{recording_id}.mp3"

        def transcript(self, recording_id):
            return f"dream {recording_id}"

    def fake_download(_url, destination, timeout_seconds=120):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"audio")

    monkeypatch.setattr(plaud_importer, "download_audio", fake_download)
    result = plaud_importer.pull_recent_recordings(
        outbox_dir=tmp_path / "outbox",
        cli=FakeCli(),
        lookback_days=30,
        recording_id="b",
    )
    assert result == {"pulled": 1, "skipped": 1, "waiting_transcript": 0}
    assert not (tmp_path / "outbox" / "a").exists()
    assert (tmp_path / "outbox" / "b" / "transcript.txt").read_text(encoding="utf-8") == "dream b"


def test_unavailable_plaud_transcript_waits_instead_of_uploading(tmp_path, monkeypatch):
    audio = tmp_path / "source.mp3"
    audio.write_bytes(b"audio")
    outbox = tmp_path / "outbox"
    plaud_importer.queue_recording(
        outbox_dir=outbox,
        metadata={"id": "plaud-empty", "start_at": "2026-06-04T06:12:00-07:00"},
        audio_path=audio,
        transcript="Transcript not available for this recording.",
    )
    assert (outbox / "plaud-empty" / "transcript.txt").read_text(encoding="utf-8") == ""
    status = plaud_importer.read_json(outbox / "plaud-empty" / "status.json")
    assert status["status"] == "waiting_transcript"

    class FakeResponse:
        def raise_for_status(self):
            return None

    posted = []

    def successful_post(*_args, **kwargs):
        posted.append(kwargs["data"])
        return FakeResponse()

    monkeypatch.setattr(plaud_importer.requests, "post", successful_post)
    result = plaud_importer.upload_pending(
        outbox_dir=outbox,
        pi_import_url="http://dreamer.local:5000/api/import/plaud",
    )
    assert result == {"uploaded": 0, "failed": 0, "waiting_transcript": 1}
    assert posted == []


def test_sync_dayone_pending_writes_transcript_without_pi(tmp_path):
    audio = tmp_path / "source.mp3"
    audio.write_bytes(b"audio")
    outbox = tmp_path / "outbox"
    plaud_importer.queue_recording(
        outbox_dir=outbox,
        metadata={
            "id": "plaud-dayone",
            "name": "Morning dream",
            "start_at": "2026-06-04T13:12:00+00:00",
        },
        audio_path=audio,
        transcript="I dreamed about a train.",
    )

    calls = []

    def fake_upsert(**kwargs):
        calls.append(kwargs)
        return {"action": "created", "entry": {"entryId": "entry-1"}}

    result = plaud_importer.sync_dayone_pending(
        outbox_dir=outbox,
        upsert_func=fake_upsert,
    )

    assert result == {"synced": 1, "failed": 0, "waiting_transcript": 0, "skipped": 0}
    assert calls[0]["dream_text"] == "I dreamed about a train."
    assert calls[0]["target_date"].isoformat() == "2026-06-04"
    assert calls[0]["dream_local_time"] == "06:12"
    assert calls[0]["idempotency_key"] == "plaud:plaud-dayone"

    status = plaud_importer.read_json(outbox / "plaud-dayone" / "status.json")
    assert status["status"] == "queued"
    assert status["dayone_status"] == "synced"
    assert status["dayone_entry_id"] == "entry-1"


def test_sync_dayone_pending_skips_synced_recording(tmp_path):
    audio = tmp_path / "source.mp3"
    audio.write_bytes(b"audio")
    outbox = tmp_path / "outbox"
    path = plaud_importer.queue_recording(
        outbox_dir=outbox,
        metadata={"id": "already-synced", "start_at": "2026-06-04T06:12:00-07:00"},
        audio_path=audio,
        transcript="I dreamed about a train.",
    )
    plaud_importer.mark_dayone_status(path, "synced", entry_id="entry-1")

    calls = []
    result = plaud_importer.sync_dayone_pending(
        outbox_dir=outbox,
        upsert_func=lambda **kwargs: calls.append(kwargs),
    )

    assert result == {"synced": 0, "failed": 0, "waiting_transcript": 0, "skipped": 1}
    assert calls == []


def test_sync_dayone_pending_waits_for_transcript(tmp_path):
    audio = tmp_path / "source.mp3"
    audio.write_bytes(b"audio")
    outbox = tmp_path / "outbox"
    plaud_importer.queue_recording(
        outbox_dir=outbox,
        metadata={"id": "no-transcript", "start_at": "2026-06-04T06:12:00-07:00"},
        audio_path=audio,
        transcript="Transcript not available.",
    )

    result = plaud_importer.sync_dayone_pending(
        outbox_dir=outbox,
        upsert_func=lambda **kwargs: None,
    )

    assert result == {"synced": 0, "failed": 0, "waiting_transcript": 1, "skipped": 0}
    status = plaud_importer.read_json(outbox / "no-transcript" / "status.json")
    assert status["dayone_status"] == "waiting_transcript"


def test_clean_plaud_transcript_removes_cli_wrapper_and_speaker_labels():
    raw = """- Fetching transcript...

Transcript: 06-05 梦境分享

[00:01 - 00:29] Speaker 1: 嗯，梦到我的奶奶家。
[00:31 - 01:09] Speaker 1: 还梦到奶奶有点不舒服。
"""
    assert plaud_importer.clean_plaud_transcript(raw) == "嗯，梦到我的奶奶家。\n还梦到奶奶有点不舒服。"


def test_plaud_cli_table_parser_extracts_files():
    output = """  ID                                  NAME                                  DATE          DURATION
  ──────────────────────────────────────────────────────────────────────────────────────────────────
  abc123                              Dream note                            2026-06-04    60s
"""
    client = plaud_importer.PlaudCliClient()
    files = client._parse_files_table(output)
    assert files == [{
        "id": "abc123",
        "name": "Dream note",
        "created_at": "2026-06-04",
    }]


def test_plaud_cli_table_parser_fails_loudly_on_unknown_format():
    client = plaud_importer.PlaudCliClient()
    with pytest.raises(plaud_importer.PlaudImporterError):
        client._parse_files_table("no recognizable rows")


def test_plaud_cli_table_parser_accepts_empty_page():
    client = plaud_importer.PlaudCliClient()
    assert client._parse_files_table("Files on this page: 0\n\nPage 1") == []
