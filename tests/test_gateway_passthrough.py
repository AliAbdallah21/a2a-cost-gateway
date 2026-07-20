"""Phase 1 smoke test: A2A traffic is actually intercepted by the gateway.

Starts the Specialist and the Gateway as real subprocesses, then drives the
Coordinator's actual client calls against the gateway's URL. Two things must
both be true, per README.md's Phase 1 goal ("prove the interception point works"):

1. The delegation still reaches TASK_STATE_COMPLETED (the gateway forwards correctly).
2. The gateway's own console output shows it actually handled the JSON-RPC POST —
   proof the traffic really transited the proxy, not just that the end-to-end
   result happens to look right.
"""

from __future__ import annotations

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


@pytest.fixture(scope="module")
def specialist_and_gateway(tmp_path_factory):
    """Launch the real Specialist, then the real Gateway in front of it, as subprocesses."""
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

    gateway_log_path = tmp_path_factory.mktemp("gateway_logs") / "gateway.log"
    gateway_log_file = gateway_log_path.open("w", encoding="utf-8")
    gateway = subprocess.Popen(
        [sys.executable, "-u", "-m", "gateway.proxy"],
        cwd=str(REPO_ROOT),
        stdout=gateway_log_file,
        stderr=subprocess.STDOUT,
    )
    time.sleep(1.5)
    if gateway.poll() is not None:
        gateway_log_file.close()
        raise RuntimeError(f"Gateway failed to start.\n{gateway_log_path.read_text()}")

    yield gateway_log_path

    gateway.terminate()
    gateway.wait()
    gateway_log_file.close()
    specialist.terminate()
    specialist.wait()


@pytest.mark.asyncio
async def test_delegation_through_gateway_reaches_completed(specialist_and_gateway):
    """Delegating through the gateway must both succeed AND demonstrably pass through it."""
    gateway_log_path = specialist_and_gateway

    card = await fetch_specialist_card(GATEWAY_URL)
    assert card.supported_interfaces[0].url == GATEWAY_URL, (
        "discovered card should advertise the gateway's own URL, not the Specialist's real one — "
        "otherwise the Coordinator would send the actual task request straight to the Specialist"
    )

    request = build_toy_task()
    outcome = await delegate_task(card, request)

    assert outcome["task_id"], "expected a non-empty task_id from the delegation"
    assert outcome["final_state"] == TaskState.TASK_STATE_COMPLETED, (
        f"expected TASK_STATE_COMPLETED, got {TaskState.Name(outcome['final_state'])}"
    )

    time.sleep(0.5)  # let the gateway's unbuffered print land in the log file
    gateway_output = gateway_log_path.read_text(encoding="utf-8")
    assert "GATEWAY :: forwarding POST /" in gateway_output, (
        "gateway must have actually logged handling the JSON-RPC POST — this is the proof "
        "the proxy sat in the traffic path, not just that the end-to-end result looked right"
    )
