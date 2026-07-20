"""Shared logging for Phase 0: one function, two destinations (console + JSONL).

Both coordinator.py and specialist.py call log_event() for every observable
event in the exchange, so the console narrative and the on-disk record can
never drift apart (see rules.md "Logging conventions").
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message as ProtobufMessage

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"

_run_log_path: Path | None = None


def _run_started_at() -> str:
    """ISO-8601-ish timestamp safe for use in a filename (no colons)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def get_run_log_path() -> Path:
    """Return this process's run log file, creating it (and logs/) on first use."""
    global _run_log_path
    if _run_log_path is None:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        _run_log_path = LOGS_DIR / f"run_{_run_started_at()}.jsonl"
    return _run_log_path


def _to_jsonable(payload: Any) -> Any:
    """Convert an a2a-sdk payload (protobuf message, dict, or primitive) to plain JSON data.

    a2a-sdk 1.1.1's types (AgentCard, Task, Message, ...) are protobuf messages
    under the hood — this is one of the concrete things Phase 0 exists to surface.
    """
    if isinstance(payload, ProtobufMessage):
        return MessageToDict(payload, preserving_proto_field_name=True)
    if isinstance(payload, dict):
        return {k: _to_jsonable(v) for k, v in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_to_jsonable(v) for v in payload]
    return payload


def log_event(event_type: str, actor: str, task_id: str, payload: Any) -> None:
    """Log one observed event to the console and to this run's JSONL file.

    event_type: one of "agent_card_fetch", "task_state_transition",
    "task_request", "task_response" (see schema.md).
    actor: "coordinator" or "specialist" — which side observed/produced this event.
    task_id: the A2A task id this event belongs to (empty string before a task exists,
    e.g. the agent_card_fetch event which precedes task creation).
    payload: the raw SDK object or dict for this event — logged in full, unmodified.
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    jsonable_payload = _to_jsonable(payload)

    record = {
        "timestamp": timestamp,
        "task_id": task_id,
        "event_type": event_type,
        "actor": actor,
        "payload": jsonable_payload,
    }

    print(f"[{timestamp}] {actor} :: {event_type} :: task={task_id or '<none>'}")
    print(json.dumps(jsonable_payload, indent=2))

    with get_run_log_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
