"""Phase 5: the TASK_STATE_FAILED path, promoted from a throwaway verification script
(deleted after manual confirmation during Phase 0) to a permanent, real test — and
checked against Phase 2's cost estimation, which had never been exercised on a
failed (artifact-less) task before.

Runs through the real Gateway, not just Specialist+Coordinator directly: Phase 1's
pass-through is already covered by test_gateway_passthrough.py, so the new ground
this test covers is specifically "does a failure flow through the gateway's cost
estimation sensibly," not "does pass-through work" (already proven elsewhere).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from a2a.types import TaskState  # noqa: E402
from agents.coordinator import build_toy_task, delegate_task, fetch_specialist_card  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
GATEWAY_URL = "http://127.0.0.1:8080"


@pytest.fixture()
def failing_specialist_and_gateway():
    logs_dir = REPO_ROOT / "logs"
    logs_before = set(logs_dir.glob("run_*.jsonl")) if logs_dir.exists() else set()

    env = {**os.environ, "SPECIALIST_SIMULATE_FAILURE": "1"}
    specialist = subprocess.Popen(
        [sys.executable, "-m", "agents.specialist"],
        cwd=str(REPO_ROOT),
        env=env,
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

    yield logs_dir, logs_before

    gateway.terminate()
    gateway.wait()
    specialist.terminate()
    specialist.wait()
    time.sleep(1.0)  # grace period for port release, same fix as test_trace_store.py


def test_failed_task_reaches_coordinator_cleanly(failing_specialist_and_gateway):
    """The already-built TASK_STATE_FAILED path, verified as a real test — not a
    deleted throwaway script this time."""
    logs_dir, logs_before = failing_specialist_and_gateway

    async def _delegate():
        card = await fetch_specialist_card(GATEWAY_URL)
        request = build_toy_task()
        return await delegate_task(card, request)

    outcome = asyncio.run(_delegate())

    assert outcome.get("rejected") is False, "a failed task is not a budget rejection"
    assert outcome["task_id"], "a failed task still gets a real task_id (unlike a rejection)"
    assert outcome["final_state"] == TaskState.TASK_STATE_FAILED

    time.sleep(0.5)
    logs_after = set(logs_dir.glob("run_*.jsonl"))
    new_logs = logs_after - logs_before
    gateway_records = None
    for log_path in new_logs:
        records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        if any(r.get("actor") == "gateway" for r in records):
            gateway_records = records
            break
    assert gateway_records is not None, "expected a new gateway log file"

    cost_records = [r for r in gateway_records if r.get("event_type") == "cost_estimate"]
    assert len(cost_records) == 1, f"expected exactly one cost_estimate for the failed task, got {cost_records}"
    payload = cost_records[0]["payload"]

    # A failed task has no artifacts — the visible output text is empty. This must
    # produce a sensible zero, not a crash or a nonsensical value: per arch.md's Phase 5
    # notes, if this were just the already-documented "estimate only covers visible
    # text" limitation showing up differently, that's not a bug. It would only be a bug
    # if this crashed, or produced something that isn't a clean, honest zero.
    assert payload["output_tokens"] == 0, "a failed task has no artifact text to tokenize"
    assert payload["input_tokens"] > 0, "the input side was still real, visible text"
    assert payload["estimated_cost_usd"] is not None
    assert payload["estimated_cost_usd"] >= 0
    assert payload["scope_note"]  # still present and honest, same as any other record
