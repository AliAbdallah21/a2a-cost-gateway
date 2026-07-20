# PRD — Phase 0: Coordinator/Specialist A2A Toy Demo

## Problem statement
When one AI agent (a "Coordinator") delegates a task to another AI agent (a "Specialist") over the A2A protocol, three things are currently invisible or unenforced:

1. **No cost attribution** — nobody knows what the delegated task actually spent (tokens/time).
2. **No budget cap** — nothing stops a delegated task from running past a reasonable spend limit.
3. **No failure visibility** — A2A treats the called agent as a black box by design, so when a delegated task fails silently, the delegating side has no insight into what broke.

This project builds a gateway to solve all three. **Phase 0 solves none of them yet** — it exists to answer a prior question: *what does A2A traffic actually look like, concretely, on the wire?* We can't meter, cap, or trace traffic we haven't first observed directly.

## Who this is for
Teams building multi-agent systems on top of the A2A protocol (using LangGraph, CrewAI, or custom agents) who need to run agent delegation in production without flying blind on cost and failures. Immediate audience for Phase 0 output: the project's own future Phase 1+ work (the proxy), and the humans (Claude Code + Claude on claude.ai) reviewing this build.

## Goals for Phase 0 specifically
- Stand up two real agents — a **Coordinator** and a **Specialist** — using the official A2A Python SDK, no framework wrappers.
- Coordinator receives a task and delegates a sub-task to Specialist using a real A2A call (not a mock, not a simulated in-process function call).
- Specialist completes the sub-task and returns a result to the Coordinator over A2A.
- Every part of the exchange is visible and logged:
  - Agent Card exchange (discovery/capability advertisement)
  - Every task lifecycle state transition (`submitted` → `working` → `completed`, or the failure path)
  - The actual request and response payloads, not summaries of them
- Prove the plumbing works end to end before any interception logic is designed.

## Non-goals (explicitly out of scope for Phase 0)
- No proxy or gateway process. Coordinator talks to Specialist directly.
- No cost/token metering.
- No budget cap enforcement.
- No structured trace store (SQLite/JSONL query layer) — Phase 0 logging is console output plus a raw dump file, nothing queryable yet.
- No retry/streaming/failure-injection test harness (that's Phase 5).
- No dashboard, no multi-framework support, no reputation scoring.

These are all real future phases (1–5, see README.md) — noted here only so scope doesn't creep into Phase 0 work.

## Success criteria — what "Phase 0 done" looks like
Phase 0 is done when, running the two agents locally:
1. The Specialist agent is reachable and serves a valid Agent Card.
2. The Coordinator fetches that Agent Card before delegating (real discovery, not hardcoded assumptions).
3. The Coordinator sends a task to the Specialist via a real A2A task request.
4. The console output shows, in order: Agent Card exchange → `submitted` → `working` → `completed` (or a clean failure path) → final result returned to the Coordinator.
5. The full raw request/response JSON payloads for at least one complete delegation are visible in the console and saved to `logs/`.
6. A basic test (per `rules.md`) confirms the delegation completes successfully — this is a pass/fail smoke test, not a full suite.
7. Nothing in `gateway/` has been touched — it stays empty until Phase 1.
