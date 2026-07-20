"""Coordinator agent — an A2A client.

Modeled on the official a2a-sdk reference sample's test_client.py
(a2a-samples/samples/python/agents/helloworld) per arch.md/design.md.
Discovers whatever agent is at COORDINATOR_TARGET_URL, delegates one toy task,
follows it through its lifecycle, and logs every step via log_event()
(see schema.md for the event shapes and design.md for the full sequence).

Phase 0 target is the Specialist directly; Phase 1+ points this at the
gateway instead (see arch.md) — same code, different URL, per the "config
change, not a rewrite" design set out in arch.md.

Phase 3: the gateway may decline to forward a request at all if its budget
check rejects it (see arch.md's Phase 3 design notes). The client raises
A2AClientError for this — confirmed live before writing this, since an
earlier version of this file had no handling for it at all and would have
crashed uncaught on a real rejection.
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx
from dotenv import load_dotenv

from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.client.errors import A2AClientError
from a2a.helpers import new_text_message
from a2a.types import AgentCard, Role, SendMessageRequest, TaskState

from agents.logging_utils import log_event

load_dotenv()

SPECIALIST_HOST = os.getenv("SPECIALIST_HOST", "127.0.0.1")
SPECIALIST_PORT = int(os.getenv("SPECIALIST_PORT", "9999"))

# Where the Coordinator sends discovery + task requests. Defaults to the Specialist
# directly (Phase 0 behavior, unchanged). Set COORDINATOR_TARGET_URL to point this at
# the Phase 1+ gateway instead — no code change needed, only config (see arch.md).
COORDINATOR_TARGET_URL = os.getenv(
    "COORDINATOR_TARGET_URL", f"http://{SPECIALIST_HOST}:{SPECIALIST_PORT}"
)

TOY_TASK_TEXT = "hello from coordinator"

TERMINAL_STATES = {TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_FAILED}


async def fetch_specialist_card(target_url: str) -> AgentCard:
    """Discover the Specialist by fetching its Agent Card from target_url (Specialist or gateway).

    Logs the raw card as received.
    """
    async with httpx.AsyncClient() as httpx_client:
        resolver = A2ACardResolver(httpx_client=httpx_client, base_url=target_url)
        card = await resolver.get_agent_card()

    log_event(event_type="agent_card_fetch", actor="coordinator", task_id="", payload=card)
    return card


def build_toy_task():
    """Construct the sub-task payload the Coordinator will delegate. Hardcoded for Phase 0."""
    message = new_text_message(TOY_TASK_TEXT, role=Role.ROLE_USER)
    return SendMessageRequest(message=message)


async def delegate_task(specialist_card: AgentCard, request: SendMessageRequest) -> dict:
    """Send the task to the Specialist and follow it through its lifecycle, logging every step.

    Returns {"rejected": True, "rejection_reason": ...} if the gateway's Phase 3 budget
    check declined the request before it ever reached the Specialist, instead of letting
    the resulting A2AClientError propagate uncaught.
    """
    log_event(event_type="task_request", actor="coordinator", task_id="", payload=request)

    config = ClientConfig(streaming=False)
    client = await create_client(agent=specialist_card, client_config=config)

    final_task_id = ""
    final_state = TaskState.TASK_STATE_UNSPECIFIED
    final_chunk = None

    try:
        async for chunk in client.send_message(request):
            kind = chunk.WhichOneof("payload")

            if kind == "task":
                final_task_id = chunk.task.id
                final_state = chunk.task.status.state
            elif kind == "status_update":
                final_task_id = chunk.status_update.task_id
                final_state = chunk.status_update.status.state
            elif kind == "artifact_update":
                final_task_id = chunk.artifact_update.task_id

            final_chunk = chunk
            log_event(
                event_type="task_state_transition",
                actor="coordinator",
                task_id=final_task_id,
                payload=chunk,
            )
    except A2AClientError as exc:
        log_event(
            event_type="task_rejected", actor="coordinator", task_id="", payload={"reason": str(exc)}
        )
        return {"task_id": "", "final_state": None, "rejected": True, "rejection_reason": str(exc)}
    finally:
        await client.close()

    # The final chunk observed above already carries the complete raw result (task
    # artifacts, or the terminal status update) — log that same object again under
    # task_response rather than re-describing it, so this event is real data too.
    log_event(
        event_type="task_response",
        actor="coordinator",
        task_id=final_task_id,
        payload=final_chunk,
    )

    return {"task_id": final_task_id, "final_state": final_state, "rejected": False}


async def run() -> dict:
    """Orchestrate one full delegation: discover -> build -> delegate -> summarize."""
    specialist_card = await fetch_specialist_card(COORDINATOR_TARGET_URL)
    request = build_toy_task()
    outcome = await delegate_task(specialist_card, request)

    if outcome.get("rejected"):
        print(f"REJECTED :: {outcome['rejection_reason']}")
    else:
        state_name = TaskState.Name(outcome["final_state"])
        print(f"DONE :: task={outcome['task_id']} :: final_state={state_name}")

    return outcome


def main() -> None:
    outcome = asyncio.run(run())
    if outcome.get("rejected") or outcome["final_state"] != TaskState.TASK_STATE_COMPLETED:
        sys.exit(1)


if __name__ == "__main__":
    main()
