"""Phase 0 smoke test: the delegation completes successfully end to end.

Starts the Specialist as a real subprocess (real A2A over HTTP, not mocked),
then runs the Coordinator's actual delegation flow against it and asserts
the task reaches TASK_STATE_COMPLETED. Per rules.md's testing expectation —
not exhaustive, just the Phase 0 pass/fail bar.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.coordinator import run  # noqa: E402
from a2a.types import TaskState  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module", autouse=True)
def specialist_server():
    """Launch agents/specialist.py as a subprocess for the duration of this test module."""
    process = subprocess.Popen(
        [sys.executable, "-m", "agents.specialist"],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(1.5)

    if process.poll() is not None:
        stdout, stderr = process.communicate()
        raise RuntimeError(
            f"Specialist failed to start.\nstdout: {stdout.decode()}\nstderr: {stderr.decode()}"
        )

    yield process

    process.terminate()
    process.wait()


@pytest.mark.asyncio
async def test_delegation_reaches_completed():
    """The Coordinator's full run() must end in TASK_STATE_COMPLETED, loudly failing otherwise."""
    outcome = await run()

    assert outcome["task_id"], "expected a non-empty task_id from the delegation"
    assert outcome["final_state"] == TaskState.TASK_STATE_COMPLETED, (
        f"expected TASK_STATE_COMPLETED, got {TaskState.Name(outcome['final_state'])}"
    )
