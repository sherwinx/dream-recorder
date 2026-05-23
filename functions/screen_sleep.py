import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests


DEFAULT_SLEEP_CONFIG = {
    "SCREEN_SLEEP_ENABLED": True,
    "SCREEN_SLEEP_TIMEOUT_SECONDS": 300,
    "SCREEN_SLEEP_MODE": "night_only",
    "LOCATION_PROVIDER": "ipwhois",
    "SOLAR_PROVIDER": "sunrise_sunset_api",
    "SOLAR_CACHE_PATH": "db/solar_schedule_cache.json",
    "DEFAULT_LATITUDE": 34.0522,
    "DEFAULT_LONGITUDE": -118.2437,
    "DEFAULT_TIMEZONE": "America/Los_Angeles",
    "SCREEN_SLEEP_BLANK_COMMAND": "wlr-randr --output HDMI-A-1 --off",
    "SCREEN_SLEEP_WAKE_COMMAND": "wlr-randr --output HDMI-A-1 --on --mode 400x1280@59.506001Hz --transform 270 --scale 1",
    "SCREEN_SLEEP_DISPLAY": ":0",
    "SCREEN_SLEEP_STATE_POLL_INTERVAL": 5,
    "GPIO_DEVICE_STATE_ENDPOINT": "/api/device_state",
    "GPIO_SCREEN_SLEEP_ENDPOINT": "/api/screen_sleep",
    "GPIO_SCREEN_WAKE_ENDPOINT": "/api/screen_wake",
}


def sleep_config_value(config, key):
    return config.get(key, DEFAULT_SLEEP_CONFIG[key])


def parse_datetime(value, tzinfo):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tzinfo)
    return parsed.astimezone(tzinfo)


class SolarScheduleManager:
    def __init__(self, config, logger=None, requests_module=requests):
        self.config = config
        self.logger = logger
        self.requests = requests_module
        self.cache_path = sleep_config_value(config, "SOLAR_CACHE_PATH")
        self.cache = self._load_cache()

    def is_night(self, now=None):
        now = now or datetime.now(timezone.utc)
        location = self.get_location()
        tzinfo = ZoneInfo(location["timezone"])
        local_now = now.astimezone(tzinfo)
        schedule = self.get_schedule(local_now.date(), location)
        sunrise = parse_datetime(schedule["sunrise"], tzinfo)
        sunset = parse_datetime(schedule["sunset"], tzinfo)
        return local_now < sunrise or local_now >= sunset

    def get_location(self):
        cached = self.cache.get("location")
        if cached and self._location_cache_is_fresh(cached):
            return cached

        try:
            if sleep_config_value(self.config, "LOCATION_PROVIDER") == "ipwhois":
                response = self.requests.get("https://ipwho.is/", timeout=10)
                response.raise_for_status()
                data = response.json()
                if data.get("success", True) is False:
                    raise ValueError(data.get("message", "ipwhois lookup failed"))
                timezone_id = data.get("timezone", {}).get("id")
                if not timezone_id:
                    raise ValueError("ipwhois response did not include timezone.id")
                location = {
                    "latitude": float(data["latitude"]),
                    "longitude": float(data["longitude"]),
                    "timezone": timezone_id,
                    "source": "ipwhois",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                self.cache["location"] = location
                self._save_cache()
                return location
        except Exception as exc:
            self._log_warning(f"Could not resolve IP location, using fallback: {exc}")

        if cached:
            return cached

        return {
            "latitude": float(sleep_config_value(self.config, "DEFAULT_LATITUDE")),
            "longitude": float(sleep_config_value(self.config, "DEFAULT_LONGITUDE")),
            "timezone": sleep_config_value(self.config, "DEFAULT_TIMEZONE"),
            "source": "default",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def get_schedule(self, date, location):
        date_key = date.isoformat()
        cached_schedule = self.cache.get("schedules", {}).get(date_key)
        if cached_schedule:
            return cached_schedule

        schedule = None
        try:
            if sleep_config_value(self.config, "SOLAR_PROVIDER") == "sunrise_sunset_api":
                schedule = self._fetch_api_schedule(date_key, location)
        except Exception as exc:
            self._log_warning(f"Could not fetch sunrise/sunset schedule, using local fallback: {exc}")

        if schedule is None:
            schedule = self._calculate_astral_schedule(date, location)

        self.cache.setdefault("schedules", {})[date_key] = schedule
        self._save_cache()
        return schedule

    def _fetch_api_schedule(self, date_key, location):
        response = self.requests.get(
            "https://api.sunrise-sunset.org/json",
            params={
                "lat": location["latitude"],
                "lng": location["longitude"],
                "date": date_key,
                "formatted": 0,
                "tzid": location["timezone"],
            },
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status") != "OK":
            raise ValueError(f"sunrise-sunset API status was {data.get('status')}")
        results = data["results"]
        return {
            "sunrise": results["sunrise"],
            "sunset": results["sunset"],
            "source": "sunrise_sunset_api",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _calculate_astral_schedule(self, date, location):
        from astral import Observer
        from astral.sun import sun

        tzinfo = ZoneInfo(location["timezone"])
        observer = Observer(latitude=location["latitude"], longitude=location["longitude"])
        sun_times = sun(observer, date=date, tzinfo=tzinfo)
        return {
            "sunrise": sun_times["sunrise"].isoformat(),
            "sunset": sun_times["sunset"].isoformat(),
            "source": "astral",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _load_cache(self):
        try:
            with open(self.cache_path, "r") as cache_file:
                return json.load(cache_file)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"location": None, "schedules": {}}

    def _save_cache(self):
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(self.cache_path, "w") as cache_file:
                json.dump(self.cache, cache_file, indent=2)
        except Exception as exc:
            self._log_warning(f"Could not save solar schedule cache: {exc}")

    def _location_cache_is_fresh(self, location):
        try:
            updated_at = parse_datetime(location["updated_at"], timezone.utc)
            return updated_at.date() == datetime.now(timezone.utc).date()
        except Exception:
            return False

    def _log_warning(self, message):
        if self.logger:
            self.logger.warning(message)


class ScreenSleepController:
    def __init__(self, config, flask_url, logger=None, requests_module=requests, time_module=None):
        import time

        self.config = config
        self.flask_url = flask_url.rstrip("/")
        self.logger = logger
        self.requests = requests_module
        self.time = time_module or time
        self.enabled = bool(sleep_config_value(config, "SCREEN_SLEEP_ENABLED"))
        self.timeout_seconds = float(sleep_config_value(config, "SCREEN_SLEEP_TIMEOUT_SECONDS"))
        self.mode = sleep_config_value(config, "SCREEN_SLEEP_MODE")
        self.state_poll_interval = float(sleep_config_value(config, "SCREEN_SLEEP_STATE_POLL_INTERVAL"))
        self.last_interaction_time = self.time.time()
        self.last_state_poll_time = 0
        self.current_app_state = "clock"
        self.is_sleeping = False
        self.solar_schedule = SolarScheduleManager(config, logger=logger, requests_module=requests_module)

    def evaluate(self):
        if not self.enabled or self.is_sleeping:
            return

        now = self.time.time()
        self._refresh_app_state(now)

        if self.current_app_state != "clock":
            self.last_interaction_time = now
            return

        if now - self.last_interaction_time < self.timeout_seconds:
            return

        if self.mode == "night_only" and not self.solar_schedule.is_night():
            return

        self.blank_screen()

    def handle_touch(self, send_original_event):
        if self.is_sleeping:
            self.wake_screen()
            return

        self.last_interaction_time = self.time.time()
        send_original_event()

    def blank_screen(self):
        if self._run_screen_command(sleep_config_value(self.config, "SCREEN_SLEEP_BLANK_COMMAND")):
            self.is_sleeping = True
            self._post_endpoint(sleep_config_value(self.config, "GPIO_SCREEN_SLEEP_ENDPOINT"))

    def wake_screen(self):
        if self._run_screen_command(sleep_config_value(self.config, "SCREEN_SLEEP_WAKE_COMMAND")):
            self.is_sleeping = False
            self.last_interaction_time = self.time.time()
            self.current_app_state = "clock"
            self._post_endpoint(sleep_config_value(self.config, "GPIO_SCREEN_WAKE_ENDPOINT"))

    def _refresh_app_state(self, now):
        if now - self.last_state_poll_time < self.state_poll_interval:
            return
        self.last_state_poll_time = now
        try:
            response = self.requests.get(
                self._url(sleep_config_value(self.config, "GPIO_DEVICE_STATE_ENDPOINT")),
                timeout=2,
            )
            response.raise_for_status()
            state = response.json().get("state")
            if state:
                self.current_app_state = state
        except Exception as exc:
            self._log_warning(f"Could not read device state: {exc}")

    def _post_endpoint(self, endpoint):
        try:
            self.requests.post(self._url(endpoint), timeout=2)
        except Exception as exc:
            self._log_warning(f"Could not notify Flask endpoint {endpoint}: {exc}")

    def _run_screen_command(self, command):
        try:
            env = os.environ.copy()
            env.setdefault("DISPLAY", sleep_config_value(self.config, "SCREEN_SLEEP_DISPLAY"))
            subprocess.run(shlex.split(command), check=True, env=env)
            return True
        except Exception as exc:
            self._log_error(f"Screen command failed ({command}): {exc}")
            return False

    def _url(self, endpoint):
        return f"{self.flask_url}{endpoint}"

    def _log_warning(self, message):
        if self.logger:
            self.logger.warning(message)

    def _log_error(self, message):
        if self.logger:
            self.logger.error(message)
