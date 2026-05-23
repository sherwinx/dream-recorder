from datetime import datetime
from zoneinfo import ZoneInfo

from functions.screen_sleep import ScreenSleepController, SolarScheduleManager


class FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeRequests:
    def __init__(self, get_responses=None):
        self.get_responses = list(get_responses or [])
        self.posts = []

    def get(self, *args, **kwargs):
        response = self.get_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def post(self, url, *args, **kwargs):
        self.posts.append(url)
        return FakeResponse({"status": "success"})


class FakeTime:
    def __init__(self, value):
        self.value = value

    def time(self):
        return self.value


def base_config(tmp_path):
    return {
        "SOLAR_CACHE_PATH": str(tmp_path / "solar_cache.json"),
        "DEFAULT_LATITUDE": 34.0522,
        "DEFAULT_LONGITUDE": -118.2437,
        "DEFAULT_TIMEZONE": "America/Los_Angeles",
        "SCREEN_SLEEP_ENABLED": True,
        "SCREEN_SLEEP_TIMEOUT_SECONDS": 300,
        "SCREEN_SLEEP_MODE": "night_only",
        "SCREEN_SLEEP_STATE_POLL_INTERVAL": 0,
        "SCREEN_SLEEP_BLANK_COMMAND": "wlr-randr --output HDMI-A-1 --off",
        "SCREEN_SLEEP_WAKE_COMMAND": "wlr-randr --output HDMI-A-1 --on --mode 400x1280@59.506001Hz --transform 270 --scale 1",
        "SCREEN_SLEEP_DISPLAY": ":0",
        "GPIO_DEVICE_STATE_ENDPOINT": "/api/device_state",
        "GPIO_SCREEN_SLEEP_ENDPOINT": "/api/screen_sleep",
        "GPIO_SCREEN_WAKE_ENDPOINT": "/api/screen_wake",
    }


def test_ip_location_success_is_cached(tmp_path):
    fake_requests = FakeRequests([
        FakeResponse({
            "latitude": 37.7749,
            "longitude": -122.4194,
            "timezone": {"id": "America/Los_Angeles"},
        })
    ])
    manager = SolarScheduleManager(base_config(tmp_path), requests_module=fake_requests)

    location = manager.get_location()

    assert location["latitude"] == 37.7749
    assert manager.cache["location"]["source"] == "ipwhois"


def test_ip_location_failure_uses_cache(tmp_path):
    config = base_config(tmp_path)
    fake_requests = FakeRequests([RuntimeError("offline")])
    manager = SolarScheduleManager(config, requests_module=fake_requests)
    manager.cache["location"] = {
        "latitude": 40.7128,
        "longitude": -74.0060,
        "timezone": "America/New_York",
        "source": "cache",
    }

    location = manager.get_location()

    assert location["timezone"] == "America/New_York"

def test_fresh_cached_location_skips_ip_lookup(tmp_path):
    fake_requests = FakeRequests([])
    manager = SolarScheduleManager(base_config(tmp_path), requests_module=fake_requests)
    manager.cache["location"] = {
        "latitude": 40.7128,
        "longitude": -74.0060,
        "timezone": "America/New_York",
        "source": "cache",
        "updated_at": datetime.now(ZoneInfo("UTC")).isoformat(),
    }

    location = manager.get_location()

    assert location["timezone"] == "America/New_York"


def test_ip_location_failure_uses_default(tmp_path):
    manager = SolarScheduleManager(
        base_config(tmp_path),
        requests_module=FakeRequests([RuntimeError("offline")]),
    )

    location = manager.get_location()

    assert location["latitude"] == 34.0522
    assert location["timezone"] == "America/Los_Angeles"
    assert location["source"] == "default"


def test_sunrise_api_success_controls_night_window(tmp_path):
    fake_requests = FakeRequests([
        FakeResponse({
            "latitude": 34.0522,
            "longitude": -118.2437,
            "timezone": {"id": "America/Los_Angeles"},
        }),
        FakeResponse({
            "status": "OK",
            "results": {
                "sunrise": "2026-05-23T06:00:00-07:00",
                "sunset": "2026-05-23T20:00:00-07:00",
            },
        }),
    ])
    manager = SolarScheduleManager(base_config(tmp_path), requests_module=fake_requests)
    tzinfo = ZoneInfo("America/Los_Angeles")

    assert manager.is_night(datetime(2026, 5, 23, 21, 0, tzinfo=tzinfo)) is True
    assert manager.is_night(datetime(2026, 5, 23, 12, 0, tzinfo=tzinfo)) is False


def test_sunrise_api_failure_uses_astral_fallback(tmp_path, monkeypatch):
    manager = SolarScheduleManager(
        base_config(tmp_path),
        requests_module=FakeRequests([
            FakeResponse({
                "latitude": 34.0522,
                "longitude": -118.2437,
                "timezone": {"id": "America/Los_Angeles"},
            }),
            RuntimeError("api offline"),
        ]),
    )
    monkeypatch.setattr(manager, "_calculate_astral_schedule", lambda date, location: {
        "sunrise": "2026-05-23T06:00:00-07:00",
        "sunset": "2026-05-23T20:00:00-07:00",
        "source": "astral",
    })

    schedule = manager.get_schedule(
        datetime(2026, 5, 23, tzinfo=ZoneInfo("America/Los_Angeles")).date(),
        manager.get_location(),
    )

    assert schedule["source"] == "astral"


def test_controller_blanks_only_at_night_in_clock_state(tmp_path, monkeypatch):
    config = base_config(tmp_path)
    fake_time = FakeTime(1000)
    fake_requests = FakeRequests([FakeResponse({"state": "clock"})])
    calls = []
    monkeypatch.setattr("functions.screen_sleep.subprocess.run", lambda cmd, check, env: calls.append(cmd))

    controller = ScreenSleepController(
        config,
        "http://localhost:5000",
        requests_module=fake_requests,
        time_module=fake_time,
    )
    monkeypatch.setattr(controller.solar_schedule, "is_night", lambda: True)
    controller.last_interaction_time = 699

    controller.evaluate()

    assert calls == [["wlr-randr", "--output", "HDMI-A-1", "--off"]]
    assert controller.is_sleeping is True
    assert fake_requests.posts == ["http://localhost:5000/api/screen_sleep"]


def test_controller_does_not_blank_during_day(tmp_path, monkeypatch):
    config = base_config(tmp_path)
    fake_time = FakeTime(1000)
    calls = []
    monkeypatch.setattr("functions.screen_sleep.subprocess.run", lambda cmd, check, env: calls.append(cmd))
    controller = ScreenSleepController(
        config,
        "http://localhost:5000",
        requests_module=FakeRequests([FakeResponse({"state": "clock"})]),
        time_module=fake_time,
    )
    monkeypatch.setattr(controller.solar_schedule, "is_night", lambda: False)
    controller.last_interaction_time = 0

    controller.evaluate()

    assert calls == []
    assert controller.is_sleeping is False


def test_controller_does_not_blank_outside_clock_state(tmp_path, monkeypatch):
    config = base_config(tmp_path)
    fake_time = FakeTime(1000)
    calls = []
    monkeypatch.setattr("functions.screen_sleep.subprocess.run", lambda cmd, check, env: calls.append(cmd))
    controller = ScreenSleepController(
        config,
        "http://localhost:5000",
        requests_module=FakeRequests([FakeResponse({"state": "recording"})]),
        time_module=fake_time,
    )
    monkeypatch.setattr(controller.solar_schedule, "is_night", lambda: True)
    controller.last_interaction_time = 0

    controller.evaluate()

    assert calls == []
    assert controller.last_interaction_time == 1000


def test_sleeping_touch_wakes_and_swallows_original_tap(tmp_path, monkeypatch):
    config = base_config(tmp_path)
    fake_requests = FakeRequests()
    calls = []
    sent = []
    monkeypatch.setattr("functions.screen_sleep.subprocess.run", lambda cmd, check, env: calls.append(cmd))
    controller = ScreenSleepController(
        config,
        "http://localhost:5000",
        requests_module=fake_requests,
        time_module=FakeTime(1000),
    )
    controller.is_sleeping = True

    controller.handle_touch(lambda: sent.append("tap"))

    assert calls == [[
        "wlr-randr",
        "--output",
        "HDMI-A-1",
        "--on",
        "--mode",
        "400x1280@59.506001Hz",
        "--transform",
        "270",
        "--scale",
        "1",
    ]]
    assert sent == []
    assert controller.is_sleeping is False
    assert fake_requests.posts == ["http://localhost:5000/api/screen_wake"]
