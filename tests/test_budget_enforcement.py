"""Phase 3 smoke test: budget enforcement actually rejects, and actually prevents forwarding.

Uses a deliberately tiny temp budget (via BUDGET_CONFIG_PATH) so the scenario is
deterministic without depending on exact tokenizer/rate arithmetic: task 1's
pre-flight input-only estimate is small enough to be allowed through, but its
*actual* recorded cost (input + output, only knowable after the Specialist
responds) already exceeds the tiny budget — demonstrating the accepted
limitation from arch.md's Phase 3 notes ("a single task can still overshoot").
Task 2 must then be rejected before ever reaching the Specialist, proving the
gate-on-the-past mechanism actually works, not just task 1 slipping through.

Checked, not assumed: Specialist's own log must show exactly one task handled
(not two) — the only way to be sure task 2 never reached it, rather than
inferring that from the Coordinator's side alone.
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

# Smaller than even a single token typically costs at any configured rate, so task 1's
# pre-flight input-only check passes, but its real (input+output) cost will not.
TINY_BUDGET_USD = 0.000001


@pytest.fixture()
def tiny_budget_config(tmp_path):
    config_path = tmp_path / "budget_config.json"
    config_path.write_text(
        json.dumps({"Specialist": {"budget_usd": TINY_BUDGET_USD}}), encoding="utf-8"
    )
    return config_path


@pytest.fixture()
def specialist_and_gateway(tiny_budget_config):
    logs_dir = REPO_ROOT / "logs"
    logs_before = set(logs_dir.glob("run_*.jsonl")) if logs_dir.exists() else set()

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

    env = {**os.environ, "BUDGET_CONFIG_PATH": str(tiny_budget_config)}
    gateway = subprocess.Popen(
        [sys.executable, "-u", "-m", "gateway.proxy"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    time.sleep(1.5)
    if gateway.poll() is not None:
        raise RuntimeError(f"Gateway failed to start.\n{gateway.stdout.read()}")

    yield specialist, gateway, logs_dir, logs_before

    gateway.terminate()
    gateway.wait()
    specialist.terminate()
    specialist.wait()


def _find_new_log_by_actor(logs_dir: Path, logs_before: set, actor: str) -> list[dict]:
    """Find the (single) new run log written by a process whose events use the given actor,
    and return its parsed records — robust to which of several new log files belongs to whom,
    same technique as test_cost_estimation.py's _find_cost_estimate_record."""
    logs_after = set(logs_dir.glob("run_*.jsonl"))
    for log_path in logs_after - logs_before:
        records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        if any(r.get("actor") == actor for r in records):
            return records
    raise AssertionError(f"no new log file found with actor={actor!r}")


def test_second_task_rejected_after_first_exhausts_tiny_budget(specialist_and_gateway):
    specialist, gateway, logs_dir, logs_before = specialist_and_gateway

    async def _one_delegation():
        card = await fetch_specialist_card(GATEWAY_URL)
        request = build_toy_task()
        return await delegate_task(card, request)

    # Task 1: pre-flight input-only estimate is small enough to pass, but its real
    # (input + output) cost will exceed the tiny budget once recorded.
    outcome_1 = asyncio.run(_one_delegation())
    assert outcome_1.get("rejected") is False, f"expected task 1 to be allowed, got {outcome_1}"
    assert outcome_1["final_state"] == TaskState.TASK_STATE_COMPLETED

    # Task 2: the gateway's running spend for "Specialist" is now over budget from task 1
    # alone — this one must be rejected before ever reaching the Specialist.
    outcome_2 = asyncio.run(_one_delegation())
    assert outcome_2.get("rejected") is True, f"expected task 2 to be rejected, got {outcome_2}"
    assert "budget" in outcome_2["rejection_reason"].lower()

    # Proof, not inference: read the Specialist's own log file (not stdout — log_event's
    # console line doesn't contain the JSON key names, only the JSONL record does) and
    # confirm exactly one task's worth of transitions was logged.
    time.sleep(0.5)  # let the specialist's JSONL write land on disk
    specialist_records = _find_new_log_by_actor(logs_dir, logs_before, actor="specialist")
    transition_count = sum(1 for r in specialist_records if r.get("event_type") == "task_state_transition")
    # Each handled task logs 4 transitions (submitted/working/artifact/completed) on the
    # specialist side (see agents/specialist.py); zero would mean neither task was handled,
    # and 8 would mean both were — only 4 (one full task) is correct here.
    assert transition_count == 4, (
        f"expected exactly one task's worth of transitions (4) logged by the Specialist, "
        f"got {transition_count} — task 2 should never have reached it.\n{specialist_records}"
    )


def test_check_budget_unit_allows_and_rejects_directly():
    """Direct unit check of the decision logic itself, isolated from the HTTP/subprocess plumbing."""
    from gateway import budget

    budget._spend_by_agent.clear()
    config = {"TestAgent": {"budget_usd": 0.00001}}

    under = budget.check_budget("TestAgent", input_only_cost_usd=0.000001, budget_config=config)
    assert under.allowed is True

    budget.record_spend("TestAgent", 0.00001)
    over = budget.check_budget("TestAgent", input_only_cost_usd=0.0, budget_config=config)
    assert over.allowed is False
    assert "exhausted" in over.reason

    unconfigured = budget.check_budget("SomeOtherAgent", input_only_cost_usd=1000.0, budget_config=config)
    assert unconfigured.allowed is True, "an agent absent from budget_config.json must be unlimited"
