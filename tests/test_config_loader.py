import json

from functions import config_loader


def test_load_config_uses_explicit_config_path(tmp_path, monkeypatch):
    config_path = tmp_path / "test-config.json"
    config_path.write_text(json.dumps({"LOG_LEVEL": "CRITICAL"}))
    monkeypatch.setenv("DREAM_RECORDER_CONFIG", str(config_path))
    monkeypatch.setattr(config_loader, "_config", None)

    assert config_loader.get_config()["LOG_LEVEL"] == "CRITICAL"
