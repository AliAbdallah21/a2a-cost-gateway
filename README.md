# A2A Cost Gateway

A metering, budget-capping, and traceability layer for [A2A](https://a2a-protocol.org) (Agent-to-Agent) protocol traffic — a transparent proxy that sits between agents that delegate tasks to each other, tracks what each delegation actually costs, stops it from overspending, and keeps a queryable record of what happened at every step.

## The problem

AI agents are starting to delegate work to other AI agents automatically, using A2A — an open protocol created by Google, now governed by the Linux Foundation, with LangGraph and CrewAI shipping native support. When Agent A hands a task to Agent B, Agent B may burn real money (LLM tokens) completing it. A2A itself has no standard way to:

1. **Attribute cost** — know exactly what a delegated task spent
2. **Cap spend** — stop a task before it blows past a budget
3. **See failures clearly** — A2A treats the callee as a black box by design, so a silent failure gives no visibility into what broke

This project is a reference implementation of a gateway that closes that gap — at the network level, not by requiring agents to adopt a library.

## Architecture

```
Coordinator  --->  [ Gateway ]  --->  Specialist
                        |
                        v
        Cost Estimate + Budget Check + Trace Log
```

The **Gateway** is a transparent reverse proxy: it discovers the Specialist's Agent Card, rewrites the advertised endpoint to point at itself, and forwards every request/response unmodified — with one deliberate exception at each layer:

| Layer | What it does | Why it's not "just a proxy" there |
|---|---|---|
| **Discovery** | Rewrites the Agent Card's endpoint URL to itself | Without this, the Coordinator would discover the gateway but send real traffic straight to the Specialist, bypassing it entirely |
| **Cost estimation** | Tokenizes the visible request/response text (self-report → provider-specific tokenizer → generic fallback, in that order of trust) | The gateway can't see inside the Specialist's own LLM calls — only what crosses the wire — so every estimate is honestly labeled with which tier produced it |
| **Budget enforcement** | Rejects a request via a JSON-RPC error *before* forwarding, if the agent's accumulated spend already exceeds its budget | A task's own cost is only knowable *after* the Specialist responds, so the gateway gates on **accumulated past spend**, never on the current task's unknowable future cost |
| **Tracing** | Every event (Agent Card fetch, task state transition, cost estimate, budget decision) is logged to JSONL by all three processes, then loadable into a queryable SQLite view | JSONL stays the canonical source of truth; SQLite is a disposable, on-demand, fully-rebuilt index over it |

Two toy agents exercise the gateway end to end:
- **Coordinator** (`agents/coordinator.py`) — an A2A client that discovers an agent and delegates a task to it.
- **Specialist** (`agents/specialist.py`) — an A2A server that does trivial, deterministic work (uppercasing input text), with opt-in flags to simulate failure, cost self-reporting, and latency for testing.

## What's implemented

- ✅ **Real A2A protocol** end to end, via the official `a2a-sdk` — no protocol reimplementation.
- ✅ **Transparent pass-through proxy** with the minimum interception needed to actually intercept traffic.
- ✅ **Tiered cost estimation** — self-reported (ground truth) → provider-specific tokenizer (`tiktoken` for OpenAI, `mistral-common` for Mistral, `google-genai`'s local tokenizer for Gemini) → generic character-based fallback. Anthropic and Llama are deliberately excluded (no credential-free, gate-free local tokenizer exists for either — see `arch.md`).
- ✅ **Budget enforcement** with an honestly documented limitation: a single task can still overshoot its budget (only knowable after the fact), and concurrent requests can race the same budget check (verified and reproduced by `tests/test_budget_race_condition.py`, not just claimed).
- ✅ **SQLite trace store**, rebuilt on demand from the JSONL event logs, that reconstructs a task's full cross-process story with one query.
- ✅ **Test suite** covering the happy path, pass-through, all three cost-estimation tiers, budget rejection, the failed-task path, trace reconstruction, and the documented concurrency race.

Deliberately **not** implemented (see `README.md`'s original build order and `arch.md` for why): streaming responses, multi-framework agent support, reputation scoring, a dashboard UI, and Anthropic/Llama tokenizer support pending a secrets-management story.

## Getting started

**Requirements:** Python 3.11+ (an Anaconda/Miniconda env named `agent-com` was used throughout development, but any virtualenv works).

```bash
# 1. Create and activate an environment
conda create -n agent-com python=3.11
conda activate agent-com

# 2. Install dependencies
pip install -r requirements.txt

# 3. (optional) copy the example env file and adjust ports if needed
cp .env.example .env
```

### Run it

Each of these runs as its own process — open a separate terminal for each, in order:

```bash
# Terminal 1 — the Specialist (an A2A server)
python -m agents.specialist

# Terminal 2 — the Gateway (proxies to the Specialist above)
python -m gateway.proxy

# Terminal 3 — the Coordinator (delegates one task through the Gateway)
COORDINATOR_TARGET_URL=http://127.0.0.1:8080 python -m agents.coordinator
```

You'll see the full A2A exchange printed live in each terminal — Agent Card discovery, task state transitions, the cost estimate, and (if you configure a tight budget) a rejection. Structured JSONL logs land in `logs/`.

To talk to the Specialist directly, skipping the gateway (useful for comparing behavior), just omit `COORDINATOR_TARGET_URL` — it defaults to the Specialist's address.

### Inspect a trace

After running a few delegations:

```bash
python -m tracing.build_trace_db
```

This rebuilds `logs/trace.db` from every `logs/run_*.jsonl` file present. Then, for example:

```sql
sqlite3 logs/trace.db "SELECT timestamp, actor, event_type FROM events WHERE task_id = '<a task id from the console output>' ORDER BY timestamp;"
```

### Run the tests

```bash
python -m pytest tests/ -v
```

The suite spins up real Specialist/Gateway subprocesses and drives real HTTP traffic through them — there are no mocks of the A2A protocol itself. A couple of tests (`test_gateway_passthrough.py`, `test_cost_estimation.py`, `test_budget_enforcement.py`, `test_budget_race_condition.py`) bind fixed local ports (`9999`, `8080`); make sure nothing else on your machine is using them.

## Configuration

All runtime configuration is environment variables (`.env`, see `.env.example`) plus a few small JSON files in `gateway/`:

| File | Purpose |
|---|---|
| `gateway/provider_config.json` | Maps an Agent Card name to the LLM provider/model backing it, for accurate (Tier 2) cost estimation |
| `gateway/rate_table.json` | Manually maintained $/1M-token pricing — no tokenizer library returns price |
| `gateway/budget_config.json` | Per-agent-name dollar budget ceiling; an agent absent from this file has no limit enforced |

## Project structure

```
agents/           Coordinator and Specialist toy agents, plus the shared JSONL logging helper
gateway/           The proxy: pass-through forwarding, cost estimation, budget enforcement
tracing/           Builds the SQLite trace store from the JSONL event logs
tests/             Real end-to-end tests (subprocess-driven, no protocol mocks)
logs/              JSONL event logs (gitignored) and the derived trace.db (gitignored)
```

## Design history

This project was built incrementally, one phase at a time, with a written design pass and live verification against the real SDK before each phase's code — not decisions made silently while implementing:

- **`prd.md`** — product requirements: the problem, goals, and success criteria for each phase
- **`arch.md`** — the architecture doc, and the most detailed record of *why*: every non-obvious design decision, every verified SDK finding, every accepted (and explicitly documented) limitation
- **`schema.md`** — concrete data shapes: Agent Cards, log events, config file formats, the trace store schema
- **`rules.md`** — engineering conventions and scope-discipline rules followed throughout
- **`design.md`** — the original Phase 0 module-level design

If you're trying to understand *why* something works the way it does — particularly the cost-estimation tiering, the budget "gate on the past, not the future" design, or any of the documented limitations — `arch.md` is the place to look first.
