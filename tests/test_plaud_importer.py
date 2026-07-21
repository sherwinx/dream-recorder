from datetime import datetime, timedelta, timezone
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


def test_upload_pending_uses_a_short_connect_timeout(tmp_path, monkeypatch):
    """An unplugged Pi drops packets silently, so a single 900s timeout hangs the
    whole daily job. Connect must give up fast while uploads keep a long read
    budget."""
    audio = tmp_path / "source.mp3"
    audio.write_bytes(b"audio")
    outbox = tmp_path / "outbox"
    plaud_importer.queue_recording(
        outbox_dir=outbox,
        metadata={"id": "slow-pi", "start_at": "2026-06-04T06:12:00+00:00"},
        audio_path=audio,
        transcript="I dreamed about a train.",
    )

    seen = {}

    def fake_post(url, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        raise RuntimeError("connection timed out")

    monkeypatch.setattr(plaud_importer.requests, "post", fake_post)
    plaud_importer.upload_pending(outbox_dir=outbox, pi_import_url="http://pi.local")

    connect_timeout, read_timeout = seen["timeout"]
    assert connect_timeout <= 15
    assert read_timeout >= 300


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


def test_pull_skips_recordings_already_complete_in_the_outbox(tmp_path):
    """Re-downloading every recording daily wasted ~2 minutes per run: the Pi is
    offline so nothing ever reaches the `uploaded` status that used to gate this."""
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    audio = tmp_path / "source.mp3"
    audio.write_bytes(b"audio")
    outbox = tmp_path / "outbox"
    plaud_importer.queue_recording(
        outbox_dir=outbox,
        metadata={"id": "complete", "start_at": recent},
        audio_path=audio,
        transcript="I dreamed about a train.",
    )

    class FakeCli:
        def list_files(self):
            return [{"id": "complete", "created_at": recent}]

        def file(self, recording_id):
            return {"id": recording_id, "start_at": recent}

        def audio_url(self, recording_id):
            raise AssertionError("must not re-download an already complete recording")

        def transcript(self, recording_id):
            raise AssertionError("must not re-fetch an already usable transcript")

    result = plaud_importer.pull_recent_recordings(
        outbox_dir=outbox, cli=FakeCli(), lookback_days=30
    )

    assert result == {"pulled": 0, "skipped": 1, "waiting_transcript": 0}


def test_pull_retries_recordings_whose_transcript_is_still_missing(tmp_path, monkeypatch):
    """Plaud transcribes asynchronously, so a recording queued without a transcript
    must keep being retried -- that is the whole point of re-pulling."""
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    audio = tmp_path / "source.mp3"
    audio.write_bytes(b"audio")
    outbox = tmp_path / "outbox"
    plaud_importer.queue_recording(
        outbox_dir=outbox,
        metadata={"id": "pending", "start_at": recent},
        audio_path=audio,
        transcript="Transcript not available.",
    )

    class FakeCli:
        def list_files(self):
            return [{"id": "pending", "created_at": recent}]

        def file(self, recording_id):
            return {"id": recording_id, "start_at": recent}

        def audio_url(self, recording_id):
            return f"https://example.test/{recording_id}.mp3"

        def transcript(self, recording_id):
            return "The transcript finally landed."

    def fake_download(_url, destination, timeout_seconds=120):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"audio")

    monkeypatch.setattr(plaud_importer, "download_audio", fake_download)
    result = plaud_importer.pull_recent_recordings(
        outbox_dir=outbox, cli=FakeCli(), lookback_days=30
    )

    assert result == {"pulled": 1, "skipped": 0, "waiting_transcript": 0}
    transcript = (outbox / "pending" / "transcript.txt").read_text(encoding="utf-8")
    assert transcript == "The transcript finally landed."


def test_pull_backfills_transcript_for_a_recording_already_uploaded_to_the_pi(tmp_path, monkeypatch):
    """`uploaded` means "sent to the Pi", which says nothing about whether we hold
    a transcript. Skipping on it stranded a real dream: Plaud transcribed it days
    after the upload and we never fetched the text."""
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    audio = tmp_path / "source.mp3"
    audio.write_bytes(b"audio")
    outbox = tmp_path / "outbox"
    path = plaud_importer.queue_recording(
        outbox_dir=outbox,
        metadata={"id": "uploaded-no-transcript", "start_at": recent},
        audio_path=audio,
        transcript="Transcript not available.",
    )
    plaud_importer.mark_status(path, "uploaded")

    class FakeCli:
        def list_files(self):
            return [{"id": "uploaded-no-transcript", "created_at": recent}]

        def file(self, recording_id):
            return {"id": recording_id, "start_at": recent}

        def audio_url(self, recording_id):
            return f"https://example.test/{recording_id}.mp3"

        def transcript(self, recording_id):
            return "I dreamed about an audit."

    def fake_download(_url, destination, timeout_seconds=120):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"audio")

    monkeypatch.setattr(plaud_importer, "download_audio", fake_download)
    result = plaud_importer.pull_recent_recordings(
        outbox_dir=outbox, cli=FakeCli(), lookback_days=30
    )

    assert result == {"pulled": 1, "skipped": 0, "waiting_transcript": 0}
    assert (outbox / "uploaded-no-transcript" / "transcript.txt").read_text(
        encoding="utf-8"
    ) == "I dreamed about an audit."


def test_pull_always_skips_recordings_marked_ignored(tmp_path):
    """`ignored` is a deliberate user decision, so it outranks any backfill."""
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    audio = tmp_path / "source.mp3"
    audio.write_bytes(b"audio")
    outbox = tmp_path / "outbox"
    path = plaud_importer.queue_recording(
        outbox_dir=outbox,
        metadata={"id": "not-a-dream", "start_at": recent},
        audio_path=audio,
        transcript="Transcript not available.",
    )
    plaud_importer.mark_status(path, "ignored")

    class FakeCli:
        def list_files(self):
            return [{"id": "not-a-dream", "created_at": recent}]

        def file(self, recording_id):
            raise AssertionError("must not touch an ignored recording")

        def audio_url(self, recording_id):
            raise AssertionError("must not touch an ignored recording")

        def transcript(self, recording_id):
            raise AssertionError("must not touch an ignored recording")

    result = plaud_importer.pull_recent_recordings(
        outbox_dir=outbox, cli=FakeCli(), lookback_days=30
    )

    assert result == {"pulled": 0, "skipped": 1, "waiting_transcript": 0}


def test_pull_recent_recordings_can_filter_recording_id(tmp_path, monkeypatch):
    # Relative to today: a hardcoded date silently ages out of the lookback
    # window and turns this test red weeks after it was written.
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()

    class FakeCli:
        def list_files(self):
            return [
                {"id": "a", "name": "Skip", "created_at": recent},
                {"id": "b", "name": "Import", "created_at": recent},
            ]

        def file(self, recording_id):
            return {"id": recording_id, "start_at": recent}

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


def sync_args(outbox: Path):
    return plaud_importer.parse_args(
        [
            "sync",
            "--outbox-dir",
            str(outbox),
            "--pi-import-url",
            "http://pi.local/api/import/plaud",
        ]
    )


def test_run_once_still_writes_dayone_when_plaud_pull_fails(tmp_path, monkeypatch):
    """A broken Plaud CLI must not block transcripts already sitting in the outbox."""
    outbox = tmp_path / "outbox"
    calls = []

    def broken_pull(**kwargs):
        raise plaud_importer.PlaudImporterError("env: node: No such file or directory")

    monkeypatch.setattr(plaud_importer, "pull_recent_recordings", broken_pull)
    monkeypatch.setattr(
        plaud_importer,
        "sync_dayone_pending",
        lambda **kwargs: calls.append("dayone") or {"synced": 1, "failed": 0},
    )
    monkeypatch.setattr(
        plaud_importer,
        "upload_pending",
        lambda **kwargs: calls.append("upload") or {"uploaded": 1, "failed": 0},
    )

    summary = plaud_importer.run_once(sync_args(outbox))

    assert calls == ["dayone", "upload"]
    assert "node" in summary["pull"]["error"]
    assert summary["dayone"] == {"synced": 1, "failed": 0}
    assert summary["upload"] == {"uploaded": 1, "failed": 0}


def test_run_once_isolates_pi_upload_failure_from_dayone(tmp_path, monkeypatch):
    """The Pi is not always online; that must not mark the Day One write as failed."""
    outbox = tmp_path / "outbox"

    monkeypatch.setattr(plaud_importer, "pull_recent_recordings", lambda **kwargs: {"queued": 0})
    monkeypatch.setattr(plaud_importer, "sync_dayone_pending", lambda **kwargs: {"synced": 2, "failed": 0})

    def offline_pi(**kwargs):
        raise plaud_importer.PlaudImporterError("connection refused")

    monkeypatch.setattr(plaud_importer, "upload_pending", offline_pi)

    summary = plaud_importer.run_once(sync_args(outbox))

    assert summary["dayone"] == {"synced": 2, "failed": 0}
    assert "connection refused" in summary["upload"]["error"]


def test_main_exits_nonzero_and_reports_when_a_step_fails(tmp_path, monkeypatch, capsys):
    """Isolation must not make failures silent -- launchd needs a nonzero exit."""
    outbox = tmp_path / "outbox"

    monkeypatch.setattr(
        plaud_importer,
        "run_once",
        lambda args: {"pull": {"error": "env: node: No such file or directory"}, "dayone": {"synced": 0}},
    )

    exit_code = plaud_importer.main(
        ["sync", "--outbox-dir", str(outbox), "--pi-import-url", "http://pi.local"]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "node" in captured.err
    assert "pull" in captured.err


def test_main_treats_offline_pi_as_best_effort_with_pi_optional(tmp_path, monkeypatch, capsys):
    """The Pi is deliberately not always on, so its absence must not cry wolf daily."""
    outbox = tmp_path / "outbox"
    monkeypatch.setattr(
        plaud_importer,
        "run_once",
        lambda args: {"pull": {"queued": 0}, "dayone": {"synced": 1}, "upload": {"error": "connection refused"}},
    )

    exit_code = plaud_importer.main(
        ["sync", "--outbox-dir", str(outbox), "--pi-import-url", "http://pi.local", "--pi-optional"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    # still visible in the log, just not an alarm
    assert "connection refused" in captured.err


def test_main_still_alarms_on_pi_failure_without_pi_optional(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    monkeypatch.setattr(
        plaud_importer,
        "run_once",
        lambda args: {"upload": {"error": "connection refused"}},
    )

    assert (
        plaud_importer.main(["sync", "--outbox-dir", str(outbox), "--pi-import-url", "http://pi.local"])
        == 1
    )


def test_main_alarms_on_dayone_failure_even_with_pi_optional(tmp_path, monkeypatch):
    """--pi-optional must only excuse the Pi step, never the Day One write."""
    outbox = tmp_path / "outbox"
    monkeypatch.setattr(
        plaud_importer,
        "run_once",
        lambda args: {"dayone": {"error": "Day One MCP timed out"}, "upload": {"error": "connection refused"}},
    )

    assert (
        plaud_importer.main(
            ["sync", "--outbox-dir", str(outbox), "--pi-import-url", "http://pi.local", "--pi-optional"]
        )
        == 1
    )


def test_main_exits_zero_when_every_step_succeeds(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    monkeypatch.setattr(plaud_importer, "run_once", lambda args: {"pull": {"queued": 0}, "dayone": {"synced": 1}})

    assert (
        plaud_importer.main(["sync", "--outbox-dir", str(outbox), "--pi-import-url", "http://pi.local"])
        == 0
    )
