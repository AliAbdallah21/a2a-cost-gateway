"""Gateway — a transparent pass-through proxy between Coordinator and Specialist,
with Phase 2 cost estimation and Phase 3 budget enforcement observing (and, for
Phase 3 only, sometimes declining to forward) the traffic that passes through it.

Per README.md Phase 1: forwards A2A traffic to the real Specialist unmodified.
The one necessary exception is the Agent Card endpoint: the served interface URL
must be rewritten to point back at this gateway, or the Coordinator would
discover the gateway but then send every actual task request straight to the
Specialist, bypassing the gateway entirely. This isn't "adding logic" — it's the
minimum required for interception to work at all (see a2a-sdk's
ClientFactory.create, which sends requests to whatever URL is in the fetched
Agent Card's supported_interfaces, not the URL used for discovery).

Phase 2 adds cost estimation (see gateway/cost_estimation.py, arch.md's Phase 2
design notes, schema.md's Phase 2 cost record shape) on top of that same
interception point: the request/response bodies are already fully buffered here
(see arch.md's Phase 1 known limitations), so Phase 2 inspects copies of those
same bytes to log a cost estimate — it never changes what's actually forwarded.

Phase 3 adds budget enforcement (see gateway/budget.py, arch.md's Phase 3 design
notes, schema.md's Phase 3 shapes). Unlike Phase 2, this DOES change what gets
forwarded: a request can be rejected with a JSON-RPC error before it ever reaches
the Specialist. The gateway can never know a task's own true cost before
forwarding it (that's only knowable after the Specialist responds) — so a task is
never rejected for what it will cost, only because prior tasks already exhausted
the budget it would draw from. Estimation/budget-checking is best-effort and must
never raise into the forwarding path with an unhandled exception.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict

import httpx
import uvicorn
from dotenv import load_dotenv
from google.protobuf.json_format import MessageToDict, ParseDict
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from a2a.client import A2ACardResolver
from a2a.server.routes import create_agent_card_routes
from a2a.types import AgentCard, Message, Task, TaskState

from agents.logging_utils import log_event
from gateway import budget, cost_estimation

load_dotenv()

# This project's own JSON-RPC error code for budget rejection — outside a2a-sdk's
# reserved range (-32001 through -32009, confirmed against a2a/utils/errors.py),
# not a JSON-RPC or A2A spec-defined code (see arch.md's Phase 3 notes).
BUDGET_REJECTED_ERROR_CODE = -32010

GATEWAY_HOST = os.getenv("GATEWAY_HOST", "127.0.0.1")
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8080"))
GATEWAY_URL = f"http://{GATEWAY_HOST}:{GATEWAY_PORT}"

SPECIALIST_HOST = os.getenv("SPECIALIST_HOST", "127.0.0.1")
SPECIALIST_PORT = int(os.getenv("SPECIALIST_PORT", "9999"))
SPECIALIST_URL = f"http://{SPECIALIST_HOST}:{SPECIALIST_PORT}"

# Hop-by-hop headers (RFC 7230 6.1) plus Host/Content-Length, which httpx recomputes
# itself for the new hop — forwarding these as-is would corrupt the proxied request/response.
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

# Phase 2/3 state, populated once in build_app() (same lifecycle as the Agent Card
# fetch/rewrite) — mirrors the existing module-level config pattern (GATEWAY_URL etc).
_AGENT_NAME: str = ""
_PROVIDER_CONFIG: dict = {}
_RATE_TABLE: dict = {}
_BUDGET_CONFIG: dict = {}


async def fetch_rewritten_agent_card() -> AgentCard:
    """Fetch the real Specialist's Agent Card and rewrite its interface URL(s) to this gateway."""
    async with httpx.AsyncClient() as httpx_client:
        resolver = A2ACardResolver(httpx_client=httpx_client, base_url=SPECIALIST_URL)
        real_card = await resolver.get_agent_card()

    gateway_card = AgentCard()
    gateway_card.CopyFrom(real_card)
    for interface in gateway_card.supported_interfaces:
        interface.url = GATEWAY_URL
    return gateway_card


def _parse_send_message(request_body: bytes) -> tuple[str, str] | None:
    """Best-effort: (json_rpc_id, input_text) if this is a SendMessage JSON-RPC request, else None.

    None covers anything not worth budget-checking or cost-observing: a different
    JSON-RPC method, or a body that doesn't parse as expected.
    """
    try:
        data = json.loads(request_body)
        if data.get("method") != "SendMessage":
            return None
        message = Message()
        ParseDict(data["params"]["message"], message)
        input_text = " ".join(part.text for part in message.parts if part.text)
        return data.get("id", ""), input_text
    except Exception:
        return None


def _extract_terminal_task(response_body: bytes) -> tuple[str, str, dict] | None:
    """Best-effort: (task_id, output_text, metadata) if the response is a terminal Task, else None.

    Non-streaming only (see arch.md's Phase 1 known limitations) — our Specialist
    always returns the complete final Task in one response, never a partial update.
    """
    try:
        data = json.loads(response_body)
        result = data.get("result")
        if not result or "task" not in result:
            return None
        task = Task()
        ParseDict(result["task"], task)
        if task.status.state not in (TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_FAILED):
            return None
        output_text = " ".join(
            part.text for artifact in task.artifacts for part in artifact.parts if part.text
        )
        metadata = MessageToDict(task, preserving_proto_field_name=True).get("metadata")
        return task.id, output_text, metadata
    except Exception:
        return None


def _observe_cost(request_body: bytes, response_body: bytes) -> None:
    """Log a Phase 2 cost record for a completed/failed task, then feed it into the Phase 3
    running spend total. Never raises — best-effort only.

    Reads copies of the already-buffered bytes forward() has anyway; never touches
    what's actually sent back to the Coordinator.
    """
    extracted = _extract_terminal_task(response_body)
    if extracted is None:
        return
    task_id, output_text, metadata = extracted
    parsed = _parse_send_message(request_body)
    input_text = parsed[1] if parsed else ""

    record = cost_estimation.estimate_cost(
        task_id=task_id,
        input_text=input_text,
        output_text=output_text,
        task_metadata=metadata,
        agent_name=_AGENT_NAME,
        provider_config=_PROVIDER_CONFIG,
        rate_table=_RATE_TABLE,
    )
    log_event(event_type="cost_estimate", actor="gateway", task_id=task_id, payload=asdict(record))
    budget.record_spend(_AGENT_NAME, record.estimated_cost_usd)


def _check_budget_before_forwarding(request_body: bytes) -> Response | None:
    """Pre-flight: returns a JSON-RPC rejection Response if over budget, else None (proceed).

    Never raises — a failure here means "allow the request", not "crash the proxy";
    Phase 3's job is to sometimes say no on purpose, not to become a new failure mode.
    """
    try:
        parsed = _parse_send_message(request_body)
        if parsed is None:
            return None
        json_rpc_id, input_text = parsed

        input_only_cost = cost_estimation.estimate_input_only_cost(
            _AGENT_NAME, input_text, _PROVIDER_CONFIG, _RATE_TABLE
        )
        decision = budget.check_budget(_AGENT_NAME, input_only_cost, _BUDGET_CONFIG)

        if decision.allowed:
            return None

        print(f"GATEWAY :: REJECTED SendMessage :: {decision.reason}")
        log_event(event_type="budget_rejection", actor="gateway", task_id="", payload=asdict(decision))

        error_body = {
            "jsonrpc": "2.0",
            "id": json_rpc_id,
            "error": {
                "code": BUDGET_REJECTED_ERROR_CODE,
                "message": f"Budget exceeded: {decision.reason}",
            },
        }
        return Response(
            content=json.dumps(error_body).encode("utf-8"),
            status_code=200,
            media_type="application/json",
        )
    except Exception:
        return None


async def forward(request: Request) -> Response:
    """Forward any other request to the real Specialist unmodified, relay the response back unmodified.

    Except: a SendMessage POST may be rejected before forwarding at all, if Phase 3's
    budget check declines it (see _check_budget_before_forwarding). That's the one
    intentional deviation from "always forward" in this whole gateway.
    """
    body = await request.body()

    if request.method == "POST" and request.url.path == "/":
        rejection = _check_budget_before_forwarding(body)
        if rejection is not None:
            return rejection

    forward_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS
    }

    print(f"GATEWAY :: forwarding {request.method} {request.url.path} -> {SPECIALIST_URL}")

    async with httpx.AsyncClient() as httpx_client:
        upstream_response = await httpx_client.request(
            request.method,
            SPECIALIST_URL + request.url.path,
            headers=forward_headers,
            content=body,
            params=list(request.query_params.multi_items()),
        )

    if request.method == "POST" and request.url.path == "/":
        try:
            _observe_cost(body, upstream_response.content)
        except Exception:
            pass  # cost estimation must never break request forwarding

    response_headers = {
        k: v for k, v in upstream_response.headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS
    }
    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=response_headers,
    )


def build_app(gateway_card: AgentCard) -> Starlette:
    """Wire the agent-card route (rewritten) and a catch-all pass-through into one app."""
    global _AGENT_NAME, _PROVIDER_CONFIG, _RATE_TABLE, _BUDGET_CONFIG
    _AGENT_NAME = gateway_card.name
    _PROVIDER_CONFIG = cost_estimation.load_provider_config()
    _RATE_TABLE = cost_estimation.load_rate_table()
    _BUDGET_CONFIG = budget.load_budget_config()

    routes = []
    routes.extend(create_agent_card_routes(gateway_card))
    routes.append(
        Route(
            "/{path:path}",
            endpoint=forward,
            methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        )
    )
    return Starlette(routes=routes)


def main() -> None:
    """Start the Gateway as a standalone transparent proxy. The Specialist must already be running."""
    gateway_card = asyncio.run(fetch_rewritten_agent_card())
    app = build_app(gateway_card)

    print(f"Gateway listening on {GATEWAY_URL} -> forwarding to {SPECIALIST_URL}")
    uvicorn.run(app, host=GATEWAY_HOST, port=GATEWAY_PORT)


if __name__ == "__main__":
    main()
