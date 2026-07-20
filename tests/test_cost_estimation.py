"""Phase 2 smoke tests: cost records actually land in the gateway's log, correctly labeled.

Two things are checked end to end (real subprocesses, real HTTP, real logged
JSONL — not just the estimation function called in isolation): Tier 1
(self-reported, when the Specialist opts in) and Tier 2 (provider tokenizer,
the default `gateway/provider_config.json` maps "Specialist" -> openai). Tier 3
(generic fallback) is pure chars//4 arithmetic with no external dependency, so
it's covered by a direct unit call instead of a third subprocess round-trip.

Every check asserts on `estimation_method` and `scope_note` specifically —
not just "a cost_estimate event exists" — since a mislabeled or missing label
is exactly the failure mode this design exists to prevent (see arch.md's
Phase 2 design notes: an estimate must never be logged as if it were a
measurement).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.coordinator import build_toy_task, delegate_task, fetch_specialist_card  # noqa: E402
from gateway import cost_estimation  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
GATEWAY_URL = "http://127.0.0.1:8080"


def _run_delegation_through_gateway(tmp_path_factory, specialist_env: dict) -> str:
    """Start Specialist (with the given extra env) + Gateway, run one real delegation,
    return the gateway's JSONL log content."""
    env = {**os.environ, **specialist_env}
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

    logs_dir = REPO_ROOT / "logs"
    before = set(logs_dir.glob("run_*.jsonl")) if logs_dir.exists() else set()

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

    try:
        import asyncio

        async def _delegate():
            card = await fetch_specialist_card(GATEWAY_URL)
            request = build_toy_task()
            return await delegate_task(card, request)

        outcome = asyncio.run(_delegate())
        assert outcome["task_id"], "expected a real task_id from the delegation"

        time.sleep(0.5)  # let the gateway's log_event write land on disk
        after = set(logs_dir.glob("run_*.jsonl"))
        new_logs = after - before
        assert new_logs, "expected at least one new run log file"

        # Three processes each get their own run log file (specialist, gateway, AND the
        # in-process Coordinator calls this test itself makes) — mtime alone can't tell
        # them apart reliably. Find the gateway's specifically by its actual content.
        for log_path in new_logs:
            content = log_path.read_text(encoding="utf-8")
            if '"actor": "gateway"' in content:
                return content
        raise AssertionError(
            f"none of the new log files were written by the gateway: {sorted(p.name for p in new_logs)}"
        )
    finally:
        gateway.terminate()
        gateway.wait()
        specialist.terminate()
        specialist.wait()


def _find_cost_estimate_record(jsonl_content: str) -> dict:
    for line in jsonl_content.splitlines():
        record = json.loads(line)
        if record.get("event_type") == "cost_estimate":
            return record
    raise AssertionError(f"no cost_estimate event found in gateway log:\n{jsonl_content}")


def test_tier2_provider_tokenizer_lands_in_real_log(tmp_path_factory):
    """Default config (Specialist -> openai:gpt-4o-mini): real tiktoken count, correctly labeled."""
    log_content = _run_delegation_through_gateway(tmp_path_factory, specialist_env={})
    record = _find_cost_estimate_record(log_content)
    payload = record["payload"]

    assert payload["estimation_method"] == "provider_tokenizer:openai:gpt-4o-mini"
    assert payload["input_tokens"] and payload["input_tokens"] > 0
    assert payload["output_tokens"] and payload["output_tokens"] > 0
    assert payload["estimated_cost_usd"] is not None
    assert payload["scope_note"] == cost_estimation.SCOPE_NOTE


def test_tier1_self_reported_lands_in_real_log(tmp_path_factory):
    """Specialist opts in to simulating a self-report: gateway must prefer it over any estimate."""
    log_content = _run_delegation_through_gateway(
        tmp_path_factory, specialist_env={"SPECIALIST_SIMULATE_COST_REPORT": "1"}
    )
    record = _find_cost_estimate_record(log_content)
    payload = record["payload"]

    assert payload["estimation_method"] == "self_reported"
    assert payload["rate_source"] == "self_reported"
    assert payload["estimated_cost_usd"] == pytest.approx(0.0001)
    assert payload["scope_note"] == cost_estimation.SCOPE_NOTE


def test_tier3_generic_fallback_when_agent_unconfigured():
    """Direct unit check: an agent absent from provider_config.json falls to Tier 3, correctly labeled."""
    record = cost_estimation.estimate_cost(
        task_id="unit-test-task",
        input_text="hello from coordinator",
        output_text="HELLO FROM COORDINATOR",
        task_metadata=None,
        agent_name="SomeOtherAgentNotInConfig",
        provider_config=cost_estimation.load_provider_config(),
        rate_table=cost_estimation.load_rate_table(),
    )

    assert record.estimation_method == "generic_fallback"
    assert record.input_tokens == len("hello from coordinator") // 4
    assert record.estimated_cost_usd is None
    assert record.scope_note == cost_estimation.SCOPE_NOTE
