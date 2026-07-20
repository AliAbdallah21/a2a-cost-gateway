"""Phase 4 smoke test: the trace store actually reconstructs a real cross-process
task chain from the JSONL logs, and rebuilding it doesn't double-count.

Reuses the real Specialist/Gateway/Coordinator round trip (same pattern as
test_gateway_passthrough.py) so the JSONL files being ingested are genuine
output from Phase 0/1/2, not fabricated fixtures — the point of this test is
proving the *ingestion* is correct, which a hand-built JSONL file wouldn't
actually exercise.
"""

from __future__ import annotations

import asyncio
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.coordinator import build_toy_task, delegate_task, fetch_specialist_card  # noqa: E402
from tracing.build_trace_db import build_trace_db  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
GATEWAY_URL = "http://127.0.0.1:8080"


@pytest.fixture()
def specialist_and_gateway():
    specialist = subprocess.Popen(
        [sys.executable, "-m", "agents.specialist"],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    time.sleep(1.5)
    if specialist.poll() is not None:
        raise RuntimeError(f"Specialist failed to start.\n{specialist.stdout.read()}")

    gateway = subprocess.Popen(
        [sys.executable, "-u", "-m", "gateway.proxy"],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    time.sleep(1.5)
    if gateway.poll() is not None:
        raise RuntimeError(f"Gateway failed to start.\n{gateway.stdout.read()}")

    yield

    gateway.terminate()
    gateway.wait()
    specialist.terminate()
    specialist.wait()
    # Grace period for the OS to actually release the bound ports before the next
    # test's fixture tries to bind them again — process exit isn't instantaneously
    # followed by socket release on every platform. Observed this fail intermittently
    # without it: two tests in this file each spin up their own Specialist/Gateway
    # pair on the same fixed ports, back to back.
    time.sleep(1.0)


async def _run_one_delegation() -> dict:
    card = await fetch_specialist_card(GATEWAY_URL)
    request = build_toy_task()
    return await delegate_task(card, request)


def test_trace_db_reconstructs_full_task_chain(specialist_and_gateway, tmp_path):
    outcome = asyncio.run(_run_one_delegation())
    task_id = outcome["task_id"]
    assert task_id, "expected a real task_id from the delegation"

    time.sleep(0.5)  # let all three processes' log_event writes land on disk

    db_path = tmp_path / "trace.db"
    event_count = build_trace_db(logs_dir=REPO_ROOT / "logs", db_path=db_path)
    assert event_count > 0

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT actor, event_type, timestamp FROM events WHERE task_id = ? ORDER BY timestamp",
            (task_id,),
        ).fetchall()
    finally:
        conn.close()

    assert rows, f"expected trace rows for task_id={task_id}"

    actors_present = {actor for actor, _, _ in rows}
    assert actors_present == {"coordinator", "specialist", "gateway"}, (
        f"expected all three actors in one task's trace, got {actors_present}: {rows}"
    )

    event_types_present = {event_type for _, event_type, _ in rows}
    assert "task_state_transition" in event_types_present
    assert "cost_estimate" in event_types_present

    timestamps = [t for _, _, t in rows]
    assert timestamps == sorted(timestamps), "ORDER BY timestamp should yield chronological order"


def test_rebuild_is_idempotent_not_accumulating(specialist_and_gateway, tmp_path):
    asyncio.run(_run_one_delegation())
    time.sleep(0.5)

    db_path = tmp_path / "trace.db"
    first_count = build_trace_db(logs_dir=REPO_ROOT / "logs", db_path=db_path)
    second_count = build_trace_db(logs_dir=REPO_ROOT / "logs", db_path=db_path)

    assert first_count == second_count, (
        "rebuilding against unchanged JSONL files must produce the same count, not "
        "accumulate — see arch.md's Phase 4 notes on why full rebuild was chosen "
        "over incremental ingestion with a dedup key"
    )

    conn = sqlite3.connect(db_path)
    try:
        row_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()
    assert row_count == second_count


def test_malformed_line_is_skipped_not_fatal(tmp_path, capsys):
    """A truncated line (a process killed mid-write, exactly what the port-race in the
    fixtures above could realistically leave behind) must not lose the whole rebuild —
    same "must never raise, best-effort only" rule every other Phase 2/3 module follows.
    """
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "run_test.jsonl").write_text(
        '{"timestamp": "2026-07-19T00:00:00+00:00", "task_id": "t1", '
        '"event_type": "task_request", "actor": "coordinator", "payload": {"x": 1}}\n'
        '{"timestamp": "2026-07-19T00:00:01+00:00", "task_id": "t1", "event_type": "task_state_transi',
        encoding="utf-8",
    )

    db_path = tmp_path / "trace.db"
    count = build_trace_db(logs_dir=logs_dir, db_path=db_path)

    assert count == 1, "the one well-formed line must still be ingested"

    stderr = capsys.readouterr().err
    assert "run_test.jsonl:2" in stderr, "the warning must identify the exact file and line number"

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT task_id, event_type FROM events").fetchall()
    finally:
        conn.close()
    assert rows == [("t1", "task_request")]
