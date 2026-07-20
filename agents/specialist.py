"""Phase 0 Specialist agent — an A2A server.

Modeled on the official a2a-sdk reference sample
(a2a-samples/samples/python/agents/helloworld) per arch.md/design.md.
Serves an Agent Card, accepts one delegated task, does trivial deterministic
toy work, and reports every state transition through log_event() so the
exchange is fully visible (see schema.md for the event shapes).
"""

from __future__ import annotations

import asyncio
import os

import uvicorn
from dotenv import load_dotenv
from starlette.applications import Starlette

from a2a.helpers import (
    get_message_text,
    new_task_from_user_message,
    new_text_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    TaskState,
)

from agents.logging_utils import log_event

load_dotenv()

SPECIALIST_HOST = os.getenv("SPECIALIST_HOST", "127.0.0.1")
SPECIALIST_PORT = int(os.getenv("SPECIALIST_PORT", "9999"))
SPECIALIST_URL = f"http://{SPECIALIST_HOST}:{SPECIALIST_PORT}"

# Phase 2 demo only: this toy Specialist does no real LLM call (just .upper()), so it has
# nothing genuine to self-report. Opt-in flag to attach a clearly-fake cost block via A2A's
# metadata field (see schema.md's self-report convention), so the gateway's "prefer
# self-reported over any estimate" logic (Tier 1) is actually exercised end to end, not
# just asserted. Off by default — never changes Phase 0/1 behavior unless explicitly set.
SPECIALIST_SIMULATE_COST_REPORT = os.getenv("SPECIALIST_SIMULATE_COST_REPORT", "0") == "1"

# Phase 5 test-only: forces SpecialistAgent.invoke() to raise, so the already-built
# TASK_STATE_FAILED path (see the except block in SpecialistAgentExecutor.execute below)
# can be exercised as a real subprocess test instead of a throwaway harness. Off by
# default — never changes Phase 0-4 behavior unless a test explicitly sets it.
SPECIALIST_SIMULATE_FAILURE = os.getenv("SPECIALIST_SIMULATE_FAILURE", "0") == "1"

# Phase 5 test-only: the toy .upper() work returns almost instantly, which makes the
# gateway's documented Phase 3 concurrency race (arch.md) unreliable to reproduce on
# purpose — the await window it depends on is too narrow to hit consistently. This
# widens it deterministically. Off by default (0 seconds) — never changes Phase 0-4
# behavior or timing unless a test explicitly sets it (see tests/test_budget_race_condition.py).
SPECIALIST_ARTIFICIAL_DELAY_SECONDS = float(os.getenv("SPECIALIST_ARTIFICIAL_DELAY_SECONDS", "0"))


def build_agent_card() -> AgentCard:
    """Construct the Specialist's Agent Card (see schema.md). Pure data, no I/O."""
    skill = AgentSkill(
        id="toy-task",
        name="Toy Task",
        description="Performs a trivial, deterministic transformation on the input for Phase 0 testing.",
        input_modes=["text/plain"],
        output_modes=["text/plain"],
        tags=["a2a", "phase0", "toy"],
        examples=["hello from coordinator"],
    )
    return AgentCard(
        name="Specialist",
        description="Toy A2A agent that completes a delegated sub-task for Phase 0 testing.",
        version="0.1.0",
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                url=SPECIALIST_URL,
                protocol_version="1.0",
            )
        ],
        skills=[skill],
    )


class SpecialistAgent:
    """The Specialist's actual toy work. Content is arbitrary — only that it's real execution."""

    async def invoke(self, user_request: str) -> str:
        """Deterministically transform the delegated input (uppercase, for visibility)."""
        if SPECIALIST_ARTIFICIAL_DELAY_SECONDS:
            await asyncio.sleep(SPECIALIST_ARTIFICIAL_DELAY_SECONDS)
        if SPECIALIST_SIMULATE_FAILURE:
            raise RuntimeError("forced failure (SPECIALIST_SIMULATE_FAILURE=1)")
        return user_request.upper()


class _LoggingEventQueue(EventQueue):
    """Wraps a real EventQueue so every event is logged exactly as the SDK constructs it.

    TaskUpdater builds TaskStatusUpdateEvent/TaskArtifactUpdateEvent internally and never
    returns them, so the only way to log the *real* object (not a hand-written summary)
    is to intercept it at the one point it's guaranteed to pass through: enqueue_event().
    """

    def __init__(self, inner: EventQueue) -> None:
        self._inner = inner
        self.last_task_id = ""

    async def enqueue_event(self, event) -> None:
        task_id = getattr(event, "task_id", "") or getattr(event, "id", "")
        if task_id:
            self.last_task_id = task_id
        log_event(
            event_type="task_state_transition",
            actor="specialist",
            task_id=task_id or self.last_task_id,
            payload=event,
        )
        await self._inner.enqueue_event(event)


class SpecialistAgentExecutor(AgentExecutor):
    """Wires SpecialistAgent into the A2A server's task lifecycle, logging every transition."""

    def __init__(self) -> None:
        self.agent = SpecialistAgent()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Handle one delegated task end to end: submitted -> working -> completed/failed."""
        logging_queue = _LoggingEventQueue(event_queue)

        if context.current_task:
            task = context.current_task
        else:
            task = new_task_from_user_message(context.message)
            await logging_queue.enqueue_event(task)

        task_updater = TaskUpdater(
            event_queue=logging_queue, task_id=task.id, context_id=task.context_id
        )

        await task_updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=new_text_message("Processing request..."),
        )

        try:
            query = get_message_text(context.message)
            result = await self.agent.invoke(user_request=query) if query else "No text input provided!"

            await task_updater.add_artifact(
                parts=[new_text_part(text=result, media_type="text/plain")]
            )
            completed_metadata = None
            if SPECIALIST_SIMULATE_COST_REPORT:
                completed_metadata = {
                    "cost": {
                        "provider": "demo",
                        "model": "demo-model",
                        "input_tokens": len(query) // 4 if query else 0,
                        "output_tokens": len(result) // 4,
                        "cost_usd": 0.0001,
                    }
                }
            await task_updater.update_status(
                state=TaskState.TASK_STATE_COMPLETED,
                message=new_text_message("Request is completed!"),
                metadata=completed_metadata,
            )
        except Exception as exc:  # noqa: BLE001 - toy work failure must reach the client as TASK_STATE_FAILED
            await task_updater.failed(
                message=new_text_message(f"Specialist failed: {exc}")
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Cancel is not supported in Phase 0."""
        raise NotImplementedError("Cancel is not supported in Phase 0.")


def main() -> None:
    """Start the Specialist as a standalone A2A server."""
    agent_card = build_agent_card()

    request_handler = DefaultRequestHandler(
        agent_executor=SpecialistAgentExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )

    routes = []
    routes.extend(create_agent_card_routes(agent_card))
    routes.extend(create_jsonrpc_routes(request_handler, "/"))

    app = Starlette(routes=routes)

    print(f"Specialist listening on {SPECIALIST_URL}")
    uvicorn.run(app, host=SPECIALIST_HOST, port=SPECIALIST_PORT)


if __name__ == "__main__":
    main()
