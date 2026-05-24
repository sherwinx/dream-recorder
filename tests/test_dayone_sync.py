from functions.dayone_sync import (
    build_cloud_job_payload,
    is_dayone_sync_enabled,
    submit_pending_dayone_sync_jobs,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse({"job": {"id": "cloud-1"}})
        self.error = error
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        if self.error:
            raise self.error
        return self.response


class FakeDreamDB:
    def __init__(self, jobs):
        self.jobs = jobs
        self.submitted = []
        self.failed = []

    def get_dayone_sync_jobs(self, statuses=("pending",), limit=None):
        return self.jobs[:limit] if limit else self.jobs

    def mark_dayone_sync_submitted(self, job_id, relay_job_id=None):
        self.submitted.append({"job_id": job_id, "relay_job_id": relay_job_id})

    def mark_dayone_sync_failed(self, job_id, error):
        self.failed.append({"job_id": job_id, "error": error})


def test_is_dayone_sync_enabled():
    assert is_dayone_sync_enabled({"DAYONE_SYNC_ENABLED": True}) is True
    assert is_dayone_sync_enabled({"DAYONE_SYNC_ENABLED": "yes"}) is True
    assert is_dayone_sync_enabled({"DAYONE_SYNC_ENABLED": False}) is False


def test_build_cloud_job_payload():
    payload = build_cloud_job_payload(
        {
            "idempotency_key": "pi:1",
            "transcript": "dream",
            "dream_local_date": "2026-05-20",
            "dream_local_time": "07:42",
            "audio_filename": "recording.wav",
        },
        config={"DAYONE_DEVICE_ID": "pi"},
    )

    assert payload == {
        "idempotency_key": "pi:1",
        "device_id": "pi",
        "transcript": "dream",
        "dream_local_date": "2026-05-20",
        "dream_local_time": "07:42",
        "audio_filename": "recording.wav",
    }


def test_submit_pending_dayone_sync_jobs_success():
    db = FakeDreamDB([
        {
            "id": 1,
            "idempotency_key": "pi:1",
            "transcript": "dream",
            "dream_local_date": "2026-05-20",
            "dream_local_time": "07:42",
            "audio_filename": "recording.wav",
        }
    ])
    session = FakeSession()

    result = submit_pending_dayone_sync_jobs(
        db,
        config={
            "DAYONE_SYNC_ENABLED": True,
            "DAYONE_RELAY_URL": "https://relay.example",
            "DAYONE_RELAY_TOKEN": "token",
            "DAYONE_DEVICE_ID": "pi",
        },
        session=session,
    )

    assert result == {"submitted": 1, "failed": 0, "skipped": False}
    assert db.submitted == [{"job_id": 1, "relay_job_id": "cloud-1"}]
    assert session.calls[0]["args"][0] == "https://relay.example/api/jobs"
    assert session.calls[0]["kwargs"]["headers"]["Authorization"] == "Bearer token"


def test_submit_pending_dayone_sync_jobs_failure_keeps_pending():
    db = FakeDreamDB([
        {
            "id": 1,
            "idempotency_key": "pi:1",
            "transcript": "dream",
            "dream_local_date": "2026-05-20",
            "dream_local_time": "07:42",
        }
    ])

    result = submit_pending_dayone_sync_jobs(
        db,
        config={
            "DAYONE_SYNC_ENABLED": True,
            "DAYONE_RELAY_URL": "https://relay.example",
            "DAYONE_RELAY_TOKEN": "token",
            "DAYONE_DEVICE_ID": "pi",
        },
        session=FakeSession(error=RuntimeError("network down")),
    )

    assert result == {"submitted": 0, "failed": 1, "skipped": False}
    assert db.submitted == []
    assert db.failed[0]["job_id"] == 1
    assert "network down" in db.failed[0]["error"]
