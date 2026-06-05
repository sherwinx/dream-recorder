import pytest

from functions.plaud_import import PlaudImportError, clean_plaud_transcript, parse_plaud_datetime, transcript_for_import


def test_parse_plaud_datetime_treats_naive_start_at_as_utc():
    parsed = parse_plaud_datetime(
        "2026-06-05T06:13:23",
        config={"DEFAULT_TIMEZONE": "America/Los_Angeles"},
    )
    assert parsed.date().isoformat() == "2026-06-04"
    assert parsed.strftime("%H:%M") == "23:13"


def test_parse_plaud_datetime_converts_offset_to_default_timezone():
    parsed = parse_plaud_datetime(
        "2026-06-04T23:13:23-07:00",
        config={"DEFAULT_TIMEZONE": "America/Los_Angeles"},
    )
    assert parsed.date().isoformat() == "2026-06-04"
    assert parsed.strftime("%H:%M") == "23:13"


def test_clean_plaud_transcript_ignores_unavailable_message():
    assert clean_plaud_transcript("Transcript not available for this recording.") == ""


def test_clean_plaud_transcript_removes_cli_wrapper_and_speaker_labels():
    raw = """- Fetching transcript...

Transcript: 06-05 梦境分享

[00:01 - 00:29] Speaker 1: 嗯，梦到我的奶奶家。
[00:31 - 01:09] Speaker 1: 还梦到奶奶有点不舒服。
"""
    assert clean_plaud_transcript(raw) == "嗯，梦到我的奶奶家。\n还梦到奶奶有点不舒服。"


def test_transcript_for_import_requires_plaud_transcript_by_default(tmp_path):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")

    with pytest.raises(PlaudImportError, match="Plaud transcript is required"):
        transcript_for_import(audio, transcript="", config={"PLAUD_REQUIRE_TRANSCRIPT": True})
