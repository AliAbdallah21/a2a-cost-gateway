# Component Design — Phase 0

This is the module/function-level plan for `agents/coordinator.py` and `agents/specialist.py`, written before any code so the shape can be reviewed. Exact `a2a-sdk` class/function names should be confirmed against the installed SDK version at implementation time — the responsibilities below are what matters.

## `agents/specialist.py`

Runs as a standalone A2A server process.

- **`build_agent_card() -> AgentCard`**
  Constructs the Specialist's Agent Card (per `schema.md`) — name, description, version, url, capabilities, skills. Pure data, no I/O.

- **`handle_task(task_input) -> TaskResult`** (exact signature depends on `a2a-sdk`'s server handler interface)
  The Specialist's actual work function. For Phase 0, does something trivial and deterministic (e.g. uppercase the input text, or reverse it) — the content of the toy work doesn't matter, only that it's real function execution triggered by a real inbound A2A task.
  Responsible for driving/reporting its own state transitions: mark `working` on start, mark `completed` (with result artifact) on success, mark `failed` on exception — using whatever the SDK's task-state API is (likely a `TaskStore`/`TaskUpdater`-style object passed in, or a return value the SDK's server loop converts into state updates).
  Every transition it triggers must go through the shared `log_event()` helper (see below) before/as it's emitted, so the state change is visible on the Specialist's side too, not just observed later from the Coordinator's side.

- **`main()`**
  Wires `build_agent_card()` and `handle_task()` into the SDK's server app (e.g. an ASGI app the SDK provides), and starts listening on a configured host/port (from `.env`, per `rules.md`). Prints a startup line ("Specialist listening on ...") so it's obvious when it's ready for the Coordinator to hit.

## `agents/coordinator.py`

Runs as a script/client process — connects out to the Specialist, does not need to serve its own Agent Card in Phase 0.

- **`fetch_specialist_card(specialist_url: str) -> AgentCard`**
  Performs A2A discovery: fetches the Specialist's Agent Card from its well-known endpoint. Logs the raw card via `log_event("agent_card_fetch", ...)` immediately on receipt — before doing anything else with it.

- **`build_toy_task() -> Message`** (or whatever the SDK's outbound message type is)
  Constructs the sub-task payload the Coordinator will delegate. Hardcoded toy content for Phase 0 — no dynamic task planning.

- **`delegate_task(specialist_url: str, task: Message) -> TaskResult`**
  Sends the task to the Specialist via the SDK's client call, then follows the task through its lifecycle (polling or streaming, whichever the SDK exposes for non-streaming-capable agents — Specialist's Agent Card sets `streaming: false`, so this is likely poll-based for Phase 0). For every state update received (`submitted`, `working`, `completed`/`failed`), calls `log_event("task_state_transition", ...)` with the raw update before moving on. On the final response, calls `log_event("task_response", ...)` with the full raw payload.

- **`main()`**
  Orchestrates the whole Phase 0 run in order: fetch card → build task → log the outbound request → delegate → log each transition as it arrives → log final result → print a clear "DONE" summary line. Exits non-zero if the task doesn't reach `completed`.

## Shared logging helper

Lives in a small shared module (e.g. `agents/logging_utils.py`) so both files use identical log shape per `schema.md` — avoids the Coordinator and Specialist drifting into two different log formats.

- **`log_event(event_type: str, actor: str, task_id: str, payload: dict) -> None`**
  1. Stamps `timestamp` (ISO 8601, our own observation time).
  2. Prints a human-readable line to console: `[<timestamp>] <actor> :: <event_type> :: task=<task_id>` followed by the pretty-printed `payload`.
  3. Appends the same event as one JSON line to the current run's `logs/run_<timestamp>.jsonl` file.
  One function, two destinations, so console and file can never disagree (per `rules.md`'s logging conventions).

## Sequence of events — one full task delegation

1. **Specialist starts** (`specialist.py main()`): builds its Agent Card, starts serving, prints "listening on `<url>`".
2. **Coordinator starts** (`coordinator.py main()`), begins the run.
3. **Coordinator fetches Agent Card** from Specialist's well-known endpoint → `log_event("agent_card_fetch", actor="coordinator", ...)` with the raw card.
4. **Coordinator builds the toy task** locally (no I/O, no log event — nothing has left the process yet).
5. **Coordinator sends the task** to Specialist → `log_event("task_request", actor="coordinator", ...)` with the raw outbound request, sent right before/at the point of the actual HTTP call.
6. **Specialist receives the task**, SDK server loop invokes `handle_task()`.
7. **Specialist transitions to `submitted`** (or the SDK may do this automatically on receipt) → Specialist-side `log_event("task_state_transition", actor="specialist", state="submitted", ...)`.
8. **Specialist transitions to `working`**, begins toy work → `log_event("task_state_transition", actor="specialist", state="working", ...)`.
9. **Coordinator observes each state update** as it polls/streams → `log_event("task_state_transition", actor="coordinator", ...)` for each one it sees (this may partially duplicate steps 7–8's content but from the observing side — that duplication is intentional, since Phase 0's goal is seeing what each side sees).
10. **Specialist finishes toy work, transitions to `completed`** with a result artifact → `log_event("task_state_transition", actor="specialist", state="completed", ...)`.
11. **Coordinator receives the final `completed` response** → `log_event("task_response", actor="coordinator", ...)` with the full raw result payload.
12. **Coordinator prints a final "DONE" summary** (task id, final state, elapsed time) and exits 0. Non-`completed` terminal state exits non-zero.

## Where logging/print statements go

- Every `log_event()` call is the *only* place console output happens for the exchange itself — no ad hoc `print()` calls scattered elsewhere duplicating or fragmenting the record. Startup/shutdown lines (server listening, run done) are the one exception, kept clearly distinct from event logging (no `[timestamp] actor :: event_type` prefix on those).
- `log_event()` is called as close as possible to the actual SDK call/response — immediately after receiving data, immediately before sending it — not batched or deferred, so console ordering matches real wire ordering.
- Both `coordinator.py` and `specialist.py` import the same `logging_utils.log_event`, so Phase 0's console output is one interleaved, chronologically accurate narrative of the whole exchange, not two separate logs to cross-reference by hand.
