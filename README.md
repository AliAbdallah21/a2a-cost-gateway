# Agent Communication Gateway — Project Brief

## One-line summary
A metering, budget-capping, and traceability layer for A2A (Agent-to-Agent) protocol traffic — a proxy that sits between agents that delegate tasks to each other and tracks exactly what it costs, stops it from overspending, and logs what happened at every step.

## The problem
AI agents are starting to delegate work to other AI agents automatically, using a real open standard called A2A (created by Google, now governed by the Linux Foundation, backed by Microsoft, AWS, IBM, Salesforce, and others). When Agent A hands a task to Agent B, Agent B may burn real money (LLM tokens) completing it. Right now the A2A protocol has no standard way to:
1. **Attribute cost** — know exactly what a delegated task spent
2. **Cap spend** — stop a task before it blows past a budget
3. **See failures clearly** — when a delegated task fails silently, there's no visibility into what broke internally (by design, A2A treats the called agent as a black box)

Every team building on A2A right now is hand-rolling their own partial solution to this. It's an acknowledged gap in the protocol itself, not a hypothetical problem.

## What already exists (don't rebuild this)
- **A2A protocol** — handles agent discovery (signed "Agent Cards"), task lifecycle (submitted → working → completed/failed/etc.), and message/artifact exchange. This part is solved and mature (v1.0, production-ready as of Jan 2026).
- **MCP** — a separate protocol for agent-to-tool connections (not agent-to-agent). Not what we're building, but agents we test against may use it internally.
- Frameworks **LangGraph** and **CrewAI** ship native A2A support already.

## What we're building
A **proxy/gateway**, not an SDK wrapper — meaning it works regardless of what framework an agent was built with (LangGraph, CrewAI, custom), since it intercepts A2A traffic at the network/protocol level rather than requiring each agent to import a library. Three core functions, built in this order:

1. **Meter** — log the actual cost (tokens/time) of every delegated task passing through
2. **Cap** — enforce a budget limit per task or per agent relationship; reject or warn when exceeded
3. **Trace** — keep a clear, queryable log of every step in a task chain, so failures are debuggable instead of silent

## Architecture (high level)
```
Agent A  --->  [ Gateway/Proxy ]  --->  Agent B
                     |
                     v
          Metering + Budget Check + Trace Log
```
- The gateway intercepts A2A task requests going out from a delegating agent and responses coming back
- It logs Agent Card exchange, task state transitions, and estimated/actual token cost per task
- It can reject a task before forwarding if it would exceed a configured budget
- All events get written to a structured, queryable log (start simple — SQLite or JSON lines — nothing fancy yet)

## Suggested stack for v1
- **Python** (matches the official A2A SDK, easiest interop)
- **FastAPI** for the proxy/gateway server itself
- **SQLite or JSON-lines file** for local logging (no need for a real database yet)
- Official **A2A Python SDK** (a2aproject/A2A on GitHub) for both the test agents and the proxy's protocol handling

## Build order — do NOT skip ahead
**Phase 0 (today's task — build this first, nothing else):**
Build the smallest possible working example with **no proxy involved yet**:
- Two toy agents using the official A2A SDK: a "Coordinator" agent and a "Specialist" agent
- Coordinator receives a task, delegates a sub-task to Specialist via A2A
- Specialist completes it and returns a result
- Print/log the full raw message exchange to the console: the Agent Card exchange, every task lifecycle state transition (submitted → working → completed), and the actual request/response payloads
- Goal: understand exactly what data is available to intercept, before building anything that intercepts it

**Phase 1 (only after Phase 0 works and is understood):**
Insert a transparent pass-through proxy between the two agents — it does nothing yet except forward traffic unmodified. Prove the interception point works before adding logic.

**Phase 2:** Add cost metering — log token/cost estimate per task through the proxy.

**Phase 3:** Add budget cap enforcement — reject a task if it would exceed a configured limit.

**Phase 4:** Add structured tracing so a failed task chain can be inspected step-by-step.

**Phase 5:** Build a test harness — simulate retries, streaming responses, and failed sub-tasks, and verify metering stays accurate under those conditions.

**Later (not now):** multi-framework support, reputation scoring, a dashboard UI.

## What to do right now
Build **Phase 0 only**. Do not build the proxy yet. Set up the folder structure below, get the two toy agents talking over real A2A, and print the full exchange so we can see exactly what we're working with.

## Suggested folder structure
```
D:\agents_communication\
  agents\
    coordinator.py
    specialist.py
  gateway\          (empty for now — Phase 1+)
  logs\
  tests\            (empty for now — Phase 5)
  README.md
  requirements.txt
```

## Notes for whoever picks this up
- This is being built and reviewed collaboratively with Claude (in claude.ai) monitoring progress and evaluating output alongside Claude Code doing the implementation.
- Prioritize correctness and clarity over speed — this becomes a metering/billing tool eventually, so wrong numbers later will matter more than missing features now.