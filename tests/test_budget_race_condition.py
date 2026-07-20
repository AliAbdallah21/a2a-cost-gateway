"""Phase 5: converts arch.md's Phase 3 concurrency-race claim from a documented
belief into a reproduced, verified fact.

IMPORTANT — read before "fixing" a failure here: this test currently PASSES by
confirming the race exists — it asserts more requests get allowed through a
tightly-budgeted agent than the budget should permit, because that's the real,
current, documented behavior (see arch.md's Phase 3 "known limitations" and
Phase 5 design notes). If Phase 3's concurrency handling is ever hardened (a
per-agent lock or compare-and-set around check-forward-record, both named in
arch.md as the real fix), this test's assertion is EXPECTED TO FLIP — that is
success, a sign the race got fixed, not a regression to chase down.

No retry logic exists anywhere in this system (verified: a2a.client.ClientConfig
has no retry field, and neither coordinator.py nor gateway/proxy.py configures a
retrying httpx transport — see arch.md). This test doesn't need any: since every
task gets a fresh task_id, there's nothing to deduplicate. What's actually being
tested is concurrent *independent* requests racing the same budget check.

Uses SPECIALIST_ARTIFICIAL_DELAY_SECONDS (opt-in, off by default everywhere else)
to deterministically widen the race window — the toy .upper() work is too fast
for concurrent requests to reliably interleave at the vulnerable point otherwise,
which would make this test flaky (sometimes catching the bug, sometimes not) —
exactly the kind of "looks like coverage but isn't trustworthy" test this project
has avoided everywhere else.
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

from agents.coordinator import build_toy_task, delegate_task, fetch_specialist_card  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
GATEWAY_URL = "http://127.0.0.1:8080"

CONCURRENT_REQUESTS = 5
ARTIFICIAL_DELAY_SECONDS = "1.0"

# One task's real cost (3 input + 7 output tokens for "hello from coordinator" against
# gpt-4o-mini rates) is ~$4.65e-6 — see the real values captured in earlier phases'
# manual demonstrations. Budgeted just over that, so sequentially exactly one request
# should be allowed and every subsequent one rejected.
TINY_BUDGET_USD = 0.000005


@pytest.fixture()
def tiny_budget_config(tmp_path):
    config_path = tmp_path / "budget_config.json"
    config_path.write_text(
        json.dumps({"Specialist": {"budget_usd": TINY_BUDGET_USD}}), encoding="utf-8"
    )
    return config_path


@pytest.fixture()
def slow_specialist_and_gateway(tiny_budget_config, tmp_path):
    # Real files, not subprocess.PIPE: 5 concurrent requests generate roughly 5x the
    # log volume of every other test's fixtures in a much shorter window, and nothing
    # here drains the pipe during normal operation (only on a startup-failure path) —
    # an unread PIPE can fill its OS buffer and make the child process BLOCK on its next
    # write(), stalling request handling entirely. That's what caused the first attempts
    # at this test to fail with client-side timeouts — not the budget/race logic itself.
    specialist_log_path = tmp_path / "specialist.log"
    gateway_log_path = tmp_path / "gateway.log"

    specialist_env = {**os.environ, "SPECIALIST_ARTIFICIAL_DELAY_SECONDS": ARTIFICIAL_DELAY_SECONDS}
    with specialist_log_path.open("w", encoding="utf-8") as specialist_log:
        specialist = subprocess.Popen(
            [sys.executable, "-m", "agents.specialist"],
            cwd=str(REPO_ROOT),
            env=specialist_env,
            stdout=specialist_log,
            stderr=subprocess.STDOUT,
        )
        time.sleep(1.5)
        if specialist.poll() is not None:
            raise RuntimeError(f"Specialist failed to start.\n{specialist_log_path.read_text()}")

        gateway_env = {**os.environ, "BUDGET_CONFIG_PATH": str(tiny_budget_config)}
        with gateway_log_path.open("w", encoding="utf-8") as gateway_log:
            gateway = subprocess.Popen(
                [sys.executable, "-u", "-m", "gateway.proxy"],
                cwd=str(REPO_ROOT),
                env=gateway_env,
                stdout=gateway_log,
                stderr=subprocess.STDOUT,
            )
            time.sleep(1.5)
            if gateway.poll() is not None:
                raise RuntimeError(f"Gateway failed to start.\n{gateway_log_path.read_text()}")

            yield

    gateway.terminate()
    gateway.wait()
    specialist.terminate()
    specialist.wait()
    time.sleep(1.0)  # grace period for port release, same fix as test_trace_store.py


async def _run_concurrent_delegations() -> list[dict]:
    # Fetch the Agent Card once, up front — the race being tested is specifically in
    # the SendMessage/budget-check path, not agent discovery. Fanning out N concurrent
    # card fetches too just adds unrelated load and risks unrelated client timeouts
    # (observed: all 5 requests timed out client-side on the first attempt at this,
    # for exactly that reason) without testing anything more.
    card = await fetch_specialist_card(GATEWAY_URL)
    return await asyncio.gather(
        *[delegate_task(card, build_toy_task()) for _ in range(CONCURRENT_REQUESTS)]
    )


def test_concurrent_requests_exceed_the_budget(slow_specialist_and_gateway):
    """Documents, reproducibly, that arch.md's Phase 3 concurrency race is real.

    Sequentially, exactly one of these would be allowed (see TINY_BUDGET_USD). Fired
    concurrently against a Specialist slowed down enough to force interleaving, more
    than one gets allowed through — the pre-flight check for request N can run before
    request 1's spend has been recorded, because nothing serializes check-forward-record
    across concurrent requests (see arch.md's Phase 3 and Phase 5 notes).
    """
    outcomes = asyncio.run(_run_concurrent_delegations())

    allowed = [o for o in outcomes if o.get("rejected") is False]
    rejected = [o for o in outcomes if o.get("rejected") is True]
    assert len(allowed) + len(rejected) == CONCURRENT_REQUESTS

    # The documented-bad outcome: more than the budget should ever permit gets through.
    # If this ever starts failing because len(allowed) == 1, that means the race got
    # fixed — see the module docstring. Do not "fix" this test to keep it passing by
    # loosening this assertion; update it to confirm the fix instead.
    assert len(allowed) > 1, (
        f"expected the known concurrency race to let more than 1 request through a "
        f"budget sized for 1, got {len(allowed)} allowed / {len(rejected)} rejected. "
        f"If Phase 3's concurrency handling was hardened, this is the improvement "
        f"arch.md called for — update this test to assert len(allowed) == 1 instead "
        f"of treating this failure as a bug to revert."
    )
