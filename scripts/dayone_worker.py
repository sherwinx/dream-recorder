#!/usr/bin/env python3
"""Small local worker for writing Dream Recorder content to Day One via MCP."""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any


DEFAULT_DAYONE_COMMAND = "/Applications/Day One.app/Contents/MacOS/dayone"
DEFAULT_JOURNAL_NAME = "\u6bcf\u65e5\u4e00\u8bb0"

DAILY_REFLECTION_TEMPLATE = """# Daily Reflection

###### What did I dream about?


###### things that happened today THAT I FELT gratitude:
1. 
2. 
3. 

###### How could I make today better
1. 

---
"""


class DayOneMCPError(RuntimeError):
    pass


class CloudRelayError(RuntimeError):
    pass


DREAM_SECTION_RE = re.compile(
    r"(?m)^#{1,6}\s+What did I dream about\?\s*$"
)
NEXT_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+")


def idempotency_marker(idempotency_key: str) -> str:
    return f"<!-- dream-recorder:{idempotency_key} -->"


def format_dream_entry(
    dream_text: str,
    *,
    dream_local_time: str | None = None,
    idempotency_key: str | None = None,
) -> str:
    parts = []
    if dream_local_time:
        parts.append(dream_local_time)
    parts.append(dream_text.strip())
    if idempotency_key:
        parts.append(idempotency_marker(idempotency_key))
    return "\n".join(parts)


@dataclass
class DayOneMCPClient:
    command: str = DEFAULT_DAYONE_COMMAND
    timeout_seconds: int = 20

    def __post_init__(self) -> None:
        self._next_id = 1
        self._proc: subprocess.Popen[str] | None = None

    def __enter__(self) -> "DayOneMCPClient":
        self._proc = subprocess.Popen(
            [self.command, "mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._initialize()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._proc.kill()

    def list_journals(self) -> list[dict[str, Any]]:
        return self._call_tool_json("list_journals", {})

    def create_entry(
        self,
        *,
        text: str,
        journal_name: str | None = None,
        date_string: str | None = None,
        tags: list[str] | None = None,
        all_day: bool = False,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"text": text, "all_day": all_day}
        if journal_name:
            args["journal_name"] = journal_name
        if date_string:
            args["date"] = date_string
        if tags:
            args["tags"] = ",".join(tags)
        return self._call_tool_json("create_entry", args)

    def get_entries(
        self,
        *,
        journal_name: str,
        start_date: str,
        end_date: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return self._call_tool_json(
            "get_entries",
            {
                "journal_names": [journal_name],
                "start_date": start_date,
                "end_date": end_date,
                "limit": limit,
            },
        )

    def update_entry(
        self,
        *,
        entry_id: str,
        text: str,
        journal_id: str | None = None,
        tags: list[str] | None = None,
        starred: bool | None = None,
        all_day: bool | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"entry_id": entry_id, "text": text}
        if journal_id:
            args["journal_id"] = journal_id
        if tags is not None:
            args["tags"] = ",".join(tags)
        if starred is not None:
            args["starred"] = starred
        if all_day is not None:
            args["all_day"] = all_day
        return self._call_tool_json("update_entry", args)

    def _initialize(self) -> None:
        request_id = self._new_id()
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "dream-recorder-dayone-worker",
                        "version": "0.1",
                    },
                },
            }
        )
        response = self._recv_until_id(request_id)
        if "error" in response:
            raise DayOneMCPError(json.dumps(response["error"], ensure_ascii=False))
        self._send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
        )

    def _call_tool_json(self, name: str, arguments: dict[str, Any]) -> Any:
        request_id = self._new_id()
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        response = self._recv_until_id(request_id)
        if "error" in response:
            raise DayOneMCPError(json.dumps(response["error"], ensure_ascii=False))

        content = response.get("result", {}).get("content", [])
        text = "\n".join(
            item.get("text", "") for item in content if item.get("type") == "text"
        )
        if not text:
            return None
        return json.loads(text)

    def _send(self, payload: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise DayOneMCPError("Day One MCP process is not running")
        self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _recv_until_id(self, wanted_id: int) -> dict[str, Any]:
        if self._proc is None or self._proc.stdout is None:
            raise DayOneMCPError("Day One MCP process is not running")

        deadline = time.time() + self.timeout_seconds
        while time.time() < deadline:
            remaining = max(0, deadline - time.time())
            ready, _, _ = select.select([self._proc.stdout], [], [], remaining)
            if not ready:
                break
            line = self._proc.stdout.readline()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == wanted_id:
                return message

        raise DayOneMCPError(f"Timed out waiting for MCP response {wanted_id}")

    def _new_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id


@dataclass
class CloudRelayClient:
    relay_url: str
    token: str
    timeout_seconds: int = 20

    def __post_init__(self) -> None:
        self.relay_url = self.relay_url.rstrip("/")

    def get_pending_jobs(self, limit: int | None = None) -> list[dict[str, Any]]:
        path = "/api/jobs/pending"
        if limit:
            path = f"{path}?limit={limit}"
        response = self._request("GET", path)
        return response.get("jobs", response if isinstance(response, list) else [])

    def complete_job(self, job_id: str | int, *, dayone_entry_id: str | None) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/jobs/{job_id}/complete",
            {"dayone_entry_id": dayone_entry_id},
        )

    def fail_job(self, job_id: str | int, *, error: str) -> dict[str, Any]:
        return self._request("POST", f"/api/jobs/{job_id}/fail", {"error": error})

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "dream-recorder-dayone-worker/1.0",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(
            f"{self.relay_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise CloudRelayError(f"{method} {path} failed: {exc.code} {error_body}") from exc
        except urllib.error.URLError as exc:
            raise CloudRelayError(f"{method} {path} failed: {exc.reason}") from exc

        if not payload:
            return {}
        return json.loads(payload)


def create_daily_reflection_entry(
    *,
    journal_name: str,
    command: str = DEFAULT_DAYONE_COMMAND,
    include_test_text: bool = False,
) -> dict[str, Any]:
    body = DAILY_REFLECTION_TEMPLATE
    if include_test_text:
        body = body.replace(
            "###### What did I dream about?\n\n",
            (
                "###### What did I dream about?\n\n"
                "Dream Recorder worker test entry created via Day One MCP.\n\n"
            ),
            1,
        )

    with DayOneMCPClient(command=command) as client:
        journals = client.list_journals()
        if journal_name not in {journal.get("name") for journal in journals}:
            raise DayOneMCPError(
                f"Journal {journal_name!r} is not accessible via Day One MCP"
            )
        return client.create_entry(
            text=body,
            journal_name=journal_name,
            tags=["dream-recorder-test"],
        )


def build_daily_reflection_body(
    dream_text: str = "",
    *,
    dream_local_time: str | None = None,
    idempotency_key: str | None = None,
) -> str:
    if not dream_text.strip():
        return DAILY_REFLECTION_TEMPLATE
    return insert_dream_into_body(
        DAILY_REFLECTION_TEMPLATE,
        dream_text,
        dream_local_time=dream_local_time,
        idempotency_key=idempotency_key,
    )[0]


def is_daily_reflection_entry(entry: dict[str, Any]) -> bool:
    body = entry.get("body", "")
    return "# Daily Reflection" in body and DREAM_SECTION_RE.search(body) is not None


def find_daily_reflection_entry(
    entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    candidates = [entry for entry in entries if is_daily_reflection_entry(entry)]
    if not candidates:
        return None
    templated = [entry for entry in candidates if entry.get("templateID")]
    return (templated or candidates)[0]


def insert_dream_into_body(
    body: str,
    dream_text: str,
    *,
    dream_local_time: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[str, bool]:
    dream = dream_text.strip()
    if not dream:
        raise DayOneMCPError("Dream text is empty")

    match = DREAM_SECTION_RE.search(body)
    if match is None:
        raise DayOneMCPError("Daily Reflection dream section was not found")

    section_start = match.end()
    if section_start < len(body) and body[section_start] == "\n":
        section_start += 1

    next_heading = NEXT_HEADING_RE.search(body, section_start)
    section_end = next_heading.start() if next_heading else len(body)
    existing_section = body[section_start:section_end]

    marker = idempotency_marker(idempotency_key) if idempotency_key else None
    if marker and marker in existing_section:
        return body, False
    if dream in existing_section:
        return body, False

    entry_text = format_dream_entry(
        dream,
        dream_local_time=dream_local_time,
        idempotency_key=idempotency_key,
    )
    if existing_section.strip():
        new_section = f"{existing_section.rstrip()}\n\n{entry_text}\n\n"
    else:
        new_section = f"\n{entry_text}\n\n"
    return body[:section_start] + new_section + body[section_end:], True


def date_window(target_date: date) -> tuple[str, str]:
    return target_date.isoformat(), (target_date + timedelta(days=1)).isoformat()


def ensure_journal_access(client: DayOneMCPClient, journal_name: str) -> str | None:
    journals = client.list_journals()
    for journal in journals:
        if journal.get("name") == journal_name:
            return journal.get("id")
    raise DayOneMCPError(f"Journal {journal_name!r} is not accessible via Day One MCP")


def upsert_daily_reflection_dream(
    *,
    dream_text: str,
    journal_name: str,
    target_date: date,
    dream_local_time: str | None = None,
    idempotency_key: str | None = None,
    command: str = DEFAULT_DAYONE_COMMAND,
) -> dict[str, Any]:
    with DayOneMCPClient(command=command) as client:
        return upsert_daily_reflection_dream_with_client(
            client=client,
            dream_text=dream_text,
            journal_name=journal_name,
            target_date=target_date,
            dream_local_time=dream_local_time,
            idempotency_key=idempotency_key,
        )


def upsert_daily_reflection_dream_with_client(
    *,
    client: DayOneMCPClient,
    dream_text: str,
    journal_name: str,
    target_date: date,
    dream_local_time: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    journal_id = ensure_journal_access(client, journal_name)
    start_date, end_date = date_window(target_date)
    entries = client.get_entries(
        journal_name=journal_name,
        start_date=start_date,
        end_date=end_date,
    )
    entry = find_daily_reflection_entry(entries)

    if entry is None:
        response = client.create_entry(
            text=build_daily_reflection_body(
                dream_text,
                dream_local_time=dream_local_time,
                idempotency_key=idempotency_key,
            ),
            journal_name=journal_name,
            date_string=f"{target_date.isoformat()}T12:00:00Z",
            tags=["dream-recorder"],
        )
        return {"action": "created", "entry": response}

    updated_body, changed = insert_dream_into_body(
        entry.get("body", ""),
        dream_text,
        dream_local_time=dream_local_time,
        idempotency_key=idempotency_key,
    )
    if not changed:
        return {"action": "skipped", "entry": {"entryId": entry.get("id")}}

    response = client.update_entry(
        entry_id=entry["id"],
        journal_id=journal_id,
        text=updated_body,
        tags=entry.get("tags", []),
        starred=entry.get("starred", False),
        all_day=entry.get("isAllDay", False),
    )
    return {"action": "updated", "entry": response}


def sync_cloud_pending_jobs(
    *,
    relay_url: str,
    mac_token: str,
    journal_name: str,
    command: str = DEFAULT_DAYONE_COMMAND,
    limit: int | None = None,
) -> dict[str, Any]:
    relay = CloudRelayClient(relay_url=relay_url, token=mac_token)
    jobs = relay.get_pending_jobs(limit=limit)
    summary = {"processed": 0, "completed": 0, "failed": 0, "jobs": []}

    for job in jobs:
        job_id = job["id"]
        try:
            result = upsert_daily_reflection_dream(
                dream_text=job["transcript"],
                journal_name=journal_name,
                target_date=date.fromisoformat(job["dream_local_date"]),
                dream_local_time=job.get("dream_local_time"),
                idempotency_key=job.get("idempotency_key"),
                command=command,
            )
            entry = result.get("entry") or {}
            entry_id = entry.get("entryId") or entry.get("id")
            relay.complete_job(job_id, dayone_entry_id=entry_id)
            summary["completed"] += 1
            summary["jobs"].append({"id": job_id, "action": result["action"], "entryId": entry_id})
        except Exception as exc:
            summary["failed"] += 1
            summary["jobs"].append({"id": job_id, "action": "failed", "error": str(exc)})
            try:
                relay.fail_job(job_id, error=str(exc))
            except Exception as relay_exc:
                summary["jobs"][-1]["relay_error"] = str(relay_exc)
        finally:
            summary["processed"] += 1

    return summary


def read_dream_text(args: argparse.Namespace) -> str:
    if args.dream_text:
        return args.dream_text
    with open(args.dream_file, "r", encoding="utf-8") as handle:
        return handle.read()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create Day One entries from Dream Recorder content via MCP."
    )
    parser.add_argument(
        "command",
        choices=["create-daily-reflection", "upsert-dream", "sync-cloud-pending"],
        help="Worker action to run.",
    )
    parser.add_argument(
        "--journal-name",
        default=DEFAULT_JOURNAL_NAME,
        help="Day One journal name to write into.",
    )
    parser.add_argument(
        "--dayone-command",
        default=DEFAULT_DAYONE_COMMAND,
        help="Path to the Day One CLI executable.",
    )
    parser.add_argument(
        "--include-test-text",
        action="store_true",
        help="Insert a visible test sentence under the dream section.",
    )
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="Target local date in YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--dream-local-time",
        help="Dream local time in HH:MM format.",
    )
    parser.add_argument(
        "--idempotency-key",
        help="Unique key used to prevent duplicate Day One insertion.",
    )
    parser.add_argument(
        "--relay-url",
        default=os.getenv("DAYONE_RELAY_URL"),
        help="Cloudflare relay base URL.",
    )
    parser.add_argument(
        "--mac-token",
        default=os.getenv("DAYONE_MAC_TOKEN"),
        help="Token for Mac worker access to the Cloudflare relay.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of cloud jobs to process.",
    )
    dream_input = parser.add_mutually_exclusive_group()
    dream_input.add_argument(
        "--dream-text",
        help="Dream transcript text to insert into the Daily Reflection dream section.",
    )
    dream_input.add_argument(
        "--dream-file",
        help="Path to a UTF-8 text file containing the dream transcript.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.command == "create-daily-reflection":
        result = create_daily_reflection_entry(
            journal_name=args.journal_name,
            command=args.dayone_command,
            include_test_text=args.include_test_text,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "upsert-dream":
        if not args.dream_text and not args.dream_file:
            raise DayOneMCPError("upsert-dream requires --dream-text or --dream-file")
        result = upsert_daily_reflection_dream(
            dream_text=read_dream_text(args),
            journal_name=args.journal_name,
            command=args.dayone_command,
            target_date=date.fromisoformat(args.date),
            dream_local_time=args.dream_local_time,
            idempotency_key=args.idempotency_key,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "sync-cloud-pending":
        if not args.relay_url:
            raise DayOneMCPError("sync-cloud-pending requires --relay-url or DAYONE_RELAY_URL")
        if not args.mac_token:
            raise DayOneMCPError("sync-cloud-pending requires --mac-token or DAYONE_MAC_TOKEN")
        result = sync_cloud_pending_jobs(
            relay_url=args.relay_url,
            mac_token=args.mac_token,
            journal_name=args.journal_name,
            command=args.dayone_command,
            limit=args.limit,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    raise DayOneMCPError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except DayOneMCPError as exc:
        print(f"dayone worker failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
