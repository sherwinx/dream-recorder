from datetime import date

from scripts.dayone_worker import (
    build_daily_reflection_body,
    find_daily_reflection_entry,
    idempotency_marker,
    insert_dream_into_body,
    sync_cloud_pending_jobs,
    upsert_daily_reflection_dream_with_client,
)


class FakeDayOneClient:
    def __init__(self, entries=None):
        self.entries = entries or []
        self.created = []
        self.updated = []

    def list_journals(self):
        return [{"id": "journal-1", "name": "每日一记"}]

    def get_entries(self, *, journal_name, start_date, end_date, limit=50):
        self.last_get_entries = {
            "journal_name": journal_name,
            "start_date": start_date,
            "end_date": end_date,
            "limit": limit,
        }
        return self.entries

    def create_entry(self, **kwargs):
        self.created.append(kwargs)
        return {"entryId": "created-entry", "journalName": kwargs["journal_name"]}

    def update_entry(self, **kwargs):
        self.updated.append(kwargs)
        return {"entryId": kwargs["entry_id"], "journalName": "每日一记"}


class FakeRelayClient:
    def __init__(self, jobs):
        self.jobs = jobs
        self.completed = []
        self.failed = []

    def get_pending_jobs(self, limit=None):
        self.last_limit = limit
        return self.jobs

    def complete_job(self, job_id, *, dayone_entry_id):
        self.completed.append({"job_id": job_id, "dayone_entry_id": dayone_entry_id})
        return {"ok": True}

    def fail_job(self, job_id, *, error):
        self.failed.append({"job_id": job_id, "error": error})
        return {"ok": True}


def test_insert_dream_into_empty_daily_reflection_section():
    updated, changed = insert_dream_into_body(
        build_daily_reflection_body(),
        "I dreamed about a library under the ocean.",
    )

    assert changed is True
    assert "What did I dream about?" in updated
    assert "I dreamed about a library under the ocean." in updated
    assert "things that happened today" in updated


def test_insert_dream_appends_to_existing_dream_section():
    body = build_daily_reflection_body("First dream.")

    updated, changed = insert_dream_into_body(body, "Second dream.")

    assert changed is True
    assert updated.index("First dream.") < updated.index("Second dream.")
    assert updated.index("Second dream.") < updated.index("things that happened today")


def test_insert_dream_skips_duplicate_text():
    body = build_daily_reflection_body("Same dream.")

    updated, changed = insert_dream_into_body(body, "Same dream.")

    assert changed is False
    assert updated == body


def test_insert_dream_includes_time_and_idempotency_marker():
    updated, changed = insert_dream_into_body(
        build_daily_reflection_body(),
        "Timed dream.",
        dream_local_time="07:42",
        idempotency_key="pi:1",
    )

    assert changed is True
    assert "07:42\nTimed dream." in updated
    assert idempotency_marker("pi:1") in updated


def test_insert_dream_skips_duplicate_idempotency_marker():
    body = build_daily_reflection_body(
        "Original dream.",
        dream_local_time="07:42",
        idempotency_key="pi:1",
    )

    updated, changed = insert_dream_into_body(
        body,
        "Changed transcript should not duplicate.",
        dream_local_time="07:43",
        idempotency_key="pi:1",
    )

    assert changed is False
    assert updated == body


def test_find_daily_reflection_prefers_templated_entry():
    plain = {"id": "plain", "body": build_daily_reflection_body()}
    templated = {
        "id": "templated",
        "body": build_daily_reflection_body(),
        "templateID": "daily-reflection",
    }

    assert find_daily_reflection_entry([plain, templated]) == templated


def test_upsert_updates_existing_daily_reflection_entry():
    entry = {
        "id": "entry-1",
        "body": build_daily_reflection_body(),
        "tags": ["existing"],
        "starred": True,
        "isAllDay": False,
    }
    client = FakeDayOneClient(entries=[entry])

    result = upsert_daily_reflection_dream_with_client(
        client=client,
        dream_text="A new dream transcript.",
        journal_name="每日一记",
        target_date=date(2026, 5, 24),
        dream_local_time="06:30",
        idempotency_key="pi:2",
    )

    assert result["action"] == "updated"
    assert client.created == []
    assert client.updated[0]["entry_id"] == "entry-1"
    assert client.updated[0]["journal_id"] == "journal-1"
    assert client.updated[0]["tags"] == ["existing"]
    assert "A new dream transcript." in client.updated[0]["text"]
    assert "06:30" in client.updated[0]["text"]
    assert idempotency_marker("pi:2") in client.updated[0]["text"]
    assert client.last_get_entries["start_date"] == "2026-05-24"
    assert client.last_get_entries["end_date"] == "2026-05-25"


def test_upsert_creates_daily_reflection_entry_when_missing():
    client = FakeDayOneClient(entries=[])

    result = upsert_daily_reflection_dream_with_client(
        client=client,
        dream_text="A brand new dream transcript.",
        journal_name="每日一记",
        target_date=date(2026, 5, 24),
        dream_local_time="08:15",
        idempotency_key="pi:3",
    )

    assert result["action"] == "created"
    assert client.updated == []
    assert client.created[0]["journal_name"] == "每日一记"
    assert client.created[0]["date_string"] == "2026-05-24T12:00:00Z"
    assert client.created[0]["tags"] == ["dream-recorder"]
    assert "Daily Reflection" in client.created[0]["text"]
    assert "A brand new dream transcript." in client.created[0]["text"]
    assert "08:15" in client.created[0]["text"]
    assert idempotency_marker("pi:3") in client.created[0]["text"]


def test_sync_cloud_pending_processes_historical_jobs(monkeypatch):
    jobs = [
        {
            "id": 10,
            "idempotency_key": "pi:old",
            "transcript": "A dream from four days ago.",
            "dream_local_date": "2026-05-20",
            "dream_local_time": "07:15",
        },
        {
            "id": 11,
            "idempotency_key": "pi:today",
            "transcript": "A dream from today.",
            "dream_local_date": "2026-05-24",
            "dream_local_time": "08:00",
        },
    ]
    relay = FakeRelayClient(jobs)
    calls = []

    monkeypatch.setattr(
        "scripts.dayone_worker.CloudRelayClient",
        lambda relay_url, token: relay,
    )

    def fake_upsert(**kwargs):
        calls.append(kwargs)
        return {"action": "updated", "entry": {"entryId": f"entry-{kwargs['target_date']}"}}

    monkeypatch.setattr("scripts.dayone_worker.upsert_daily_reflection_dream", fake_upsert)

    result = sync_cloud_pending_jobs(
        relay_url="https://relay.example",
        mac_token="token",
        journal_name="每日一记",
    )

    assert result["processed"] == 2
    assert result["completed"] == 2
    assert calls[0]["target_date"] == date(2026, 5, 20)
    assert calls[0]["dream_local_time"] == "07:15"
    assert calls[0]["idempotency_key"] == "pi:old"
    assert calls[1]["target_date"] == date(2026, 5, 24)
    assert relay.completed == [
        {"job_id": 10, "dayone_entry_id": "entry-2026-05-20"},
        {"job_id": 11, "dayone_entry_id": "entry-2026-05-24"},
    ]


def test_sync_cloud_pending_marks_failed_job_and_continues(monkeypatch):
    relay = FakeRelayClient([
        {
            "id": 10,
            "idempotency_key": "pi:bad",
            "transcript": "bad",
            "dream_local_date": "2026-05-20",
            "dream_local_time": "07:15",
        },
        {
            "id": 11,
            "idempotency_key": "pi:good",
            "transcript": "good",
            "dream_local_date": "2026-05-21",
            "dream_local_time": "08:00",
        },
    ])
    monkeypatch.setattr(
        "scripts.dayone_worker.CloudRelayClient",
        lambda relay_url, token: relay,
    )

    def fake_upsert(**kwargs):
        if kwargs["idempotency_key"] == "pi:bad":
            raise RuntimeError("Day One failed")
        return {"action": "created", "entry": {"entryId": "entry-good"}}

    monkeypatch.setattr("scripts.dayone_worker.upsert_daily_reflection_dream", fake_upsert)

    result = sync_cloud_pending_jobs(
        relay_url="https://relay.example",
        mac_token="token",
        journal_name="每日一记",
    )

    assert result["processed"] == 2
    assert result["failed"] == 1
    assert result["completed"] == 1
    assert relay.failed[0]["job_id"] == 10
    assert "Day One failed" in relay.failed[0]["error"]
    assert relay.completed == [{"job_id": 11, "dayone_entry_id": "entry-good"}]
