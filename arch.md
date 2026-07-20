# Architecture — Phase 0 / Phase 1

## System diagram

**Phase 0 (this phase — no proxy):**
```
+--------------+                                   +--------------+
|              |   1. GET Agent Card (discovery)   |              |
|              |----------------------------------->              |
|              |                                   |              |
|              |   2. Agent Card returned          |              |
|  Coordinator |<-----------------------------------  Specialist  |
|    agent     |                                   |    agent     |
|  (client)    |   3. Task request (send message)  |  (A2A server)|
|              |----------------------------------->              |
|              |                                   |              |
|              |   4. Task state stream/updates:   |              |
|              |      submitted -> working ->      |              |
|              |      completed (+ result)         |              |
|              |<-----------------------------------              |
+--------------+                                   +--------------+
   direct HTTP, real A2A protocol, no intermediary
```

**Phase 1 (built — transparent pass-through only):** a gateway process (`gateway/proxy.py`) now sits in the middle. The Coordinator talks to it instead of to the Specialist directly:
```
Coordinator  --->  [ Gateway/Proxy ]  --->  Specialist
                          |
                          v
             Metering + Budget Check + Trace Log
                (Phase 2/3/4 — not built yet)
```
The Coordinator's outbound URL becomes the gateway's URL instead of the Specialist's real URL — set via `COORDINATOR_TARGET_URL` (see `.env.example`), no code change. The gateway forwards traffic transparently in Phase 1; metering (Phase 2), budget enforcement (Phase 3), and structured tracing (Phase 4) land on top of this same interception point later. See "Phase 1 implementation notes" below for what "transparent" actually required in practice.

## Component list

### Specialist agent (`agents/specialist.py`)
- An A2A **server**: exposes an Agent Card (its capabilities, skills, endpoint) and an A2A-compliant HTTP endpoint that accepts task requests.
- Owns one task-handling function: receive a task, do trivial toy work (e.g. echo/transform input), report state transitions (`submitted` → `working` → `completed`), and return a result artifact.
- Responsible for: serving its Agent Card correctly, following A2A task lifecycle semantics, returning a well-formed response.

### Coordinator agent (`agents/coordinator.py`)
- An A2A **client** (and conceptually could also be a server, but for Phase 0 it only needs client behavior).
- Responsible for: discovering the Specialist (fetch its Agent Card), constructing a task request, sending it, watching the task through its lifecycle, and printing/logging every step of that exchange.
- This is also where the "orchestration decision" would live in a real system (what to delegate, to whom) — for Phase 0 that decision is hardcoded (always delegate a fixed toy task to the one Specialist).

### What the A2A SDK handles vs. what we write
| Handled by `a2a-sdk` | Written by us |
|---|---|
| Agent Card schema/serialization | The actual capability description content for each toy agent |
| A2A HTTP transport, request/response wire format | Task content (the toy payload being delegated) |
| Task lifecycle state machine (valid states/transitions) | Deciding when Specialist moves `submitted` → `working` → `completed` |
| Client-side helpers for sending tasks and following state updates | Console logging / raw payload dump of every step |
| Server-side request routing to our handler function | Our handler function's actual logic (toy work) |

We are not reimplementing any part of the A2A protocol itself — the SDK is the only thing that speaks A2A. Our code is glue: agent definitions, task-handling logic, and observability (logging) around SDK calls.

## Tech stack and why
- **Python** — matches the official A2A SDK (`a2a-sdk`, from `a2aproject/a2a-python`), which is the reference implementation and has the most direct interop with the spec. No translation layer needed.
- **Official A2A Python SDK (`a2a-sdk`)** — implements A2A Protocol Specification 1.0. Using the official SDK (not a third-party reimplementation) means Phase 0's observations about "what data is available to intercept" will hold for any spec-compliant agent in later phases, not just our toy ones.
- **Starlette (not FastAPI)** for the Phase 1 gateway — README originally suggested FastAPI for the gateway. In practice, `a2a-sdk[http-server]`'s own route helpers (`create_agent_card_routes`, `create_jsonrpc_routes`, used by `agents/specialist.py`) are Starlette `Route` objects, and the gateway needed to reuse `create_agent_card_routes` directly (see implementation notes below) rather than reimplement agent-card serialization by hand. Using Starlette keeps the gateway on the same HTTP framework as the SDK's own server plumbing, with one fewer moving part. Revisit FastAPI if a later phase (e.g. Phase 2+ metering endpoints, or an eventual dashboard) needs something FastAPI gives that Starlette doesn't.
- **SQLite / JSON-lines** — not used in Phase 0 (console + flat log file is enough to observe the exchange). Named here only as the confirmed direction for Phase 4 tracing, so Phase 0's raw log format (see `schema.md`) is chosen to be easy to later replay into either.

## Proxy/gateway approach over SDK-wrapper approach — and why
The project chose to build a **network-level proxy** (a process both agents talk to) rather than a **library/SDK wrapper** (code each agent must import and call).

Why this matters for Phase 0 specifically: it means the Coordinator and Specialist must be built as two genuinely separate processes talking over real HTTP/A2A — not as two Python objects calling each other's methods in-process. A proxy can only intercept traffic that actually crosses a network boundary. If Phase 0 took a shortcut (in-process calls, mocked transport), it would teach us nothing about what the Phase 1 proxy will actually see on the wire, and the whole point of Phase 0 is to observe that traffic before designing the interceptor.

Reasons for proxy-over-wrapper (broader project rationale, not just Phase 0):
- **Framework-agnostic** — works whether the agent was built with LangGraph, CrewAI, or fully custom code, since it doesn't require the agent's implementation to import anything.
- **No cooperation required from agent authors** — teams don't have to adopt a new SDK or refactor their agent to get metering/budgets/tracing; they just point traffic at the gateway.
- **Matches how A2A already works** — A2A is transport-level (HTTP) by design, so intercepting at that layer is the natural fit rather than fighting the protocol's own architecture.

## Phase 1 implementation notes

**"Transparent" turned out to require one deliberate rewrite, not zero.** README describes Phase 1 as a proxy that "does nothing yet except forward traffic unmodified." Building it surfaced a fact that isn't obvious from the protocol description alone: `a2a-sdk`'s client (`ClientFactory.create`, in `a2a.client.client_factory`) sends every task request to whatever URL is inside the fetched Agent Card's `supported_interfaces` — **not** the URL that was used for discovery. So if the gateway only relayed bytes unmodified, the Coordinator would discover the gateway (`GET /.well-known/agent-card.json`) but then send the actual `POST /` task request straight to the Specialist's real address, bypassing the gateway completely — the interception point would exist but never actually see traffic.

The fix (`gateway/proxy.py::fetch_rewritten_agent_card`): the gateway fetches the Specialist's real card once at startup, copies it, and overwrites `supported_interfaces[].url` to point at itself. Every other route (`POST /` and anything else) is forwarded byte-for-byte via `httpx`, with hop-by-hop headers (`Connection`, `Host`, `Content-Length`, etc. — RFC 7230 §6.1) stripped and recomputed per hop, same as any standard reverse proxy. This is the minimum change needed for interception to work at all — it's not "adding logic" in the Phase 2+ sense (no cost/budget/trace decisions are made), so it doesn't violate the Phase 1 scope boundary in `rules.md`.

**Verified, not just asserted:** `tests/test_gateway_passthrough.py` checks two things, not one — that the delegation still reaches `TASK_STATE_COMPLETED`, *and* that the gateway's own process log actually contains `GATEWAY :: forwarding POST /`, i.e. that the JSON-RPC request demonstrably transited the proxy rather than the Coordinator having reached the Specialist directly.

## Phase 1 known limitations — flags for later, not bugs now

Not fixes — Phase 1's scope is pass-through only, and none of these affect that goal. Written down so Phase 2+ doesn't rediscover them the hard way:

- **Bodies are fully buffered, not streamed.** `forward()` does `await request.body()` and reads `upstream_response.content` in full before relaying either direction. Fine for the toy text payloads Phase 0/1 use; would need reworking to `httpx.AsyncClient().stream(...)` if a delegated task ever carries a large file or a long response.
- **No SSE/streaming-response handling.** The Specialist's Agent Card declares `capabilities.streaming: false`, so this has never been exercised. The proxy has no logic for chunked/event-stream responses — a real gap, not a tested-and-fine case, if a future Specialist enables streaming.
- **The rewritten Agent Card is fetched once, at gateway startup, and cached in memory.** If the real Specialist's card changed at runtime (new skills, moved endpoint), the gateway wouldn't notice until restarted. Not a concern at this scale (one static toy agent) but worth remembering once agents become dynamic.

## Phase 2 design — cost estimation (not implemented yet)

The gateway only ever sees the A2A envelope (input message, output artifact) — never the Specialist's internal LLM calls, system prompts, or tool use. So "cost metering" here can only ever be one of: a number the Specialist voluntarily self-reports (ground truth), an estimate derived from tokenizing the *visible* text (a lower bound, not true spend), or a generic fallback when neither applies. Every logged record is labeled with which of the three produced it (see `schema.md`'s Phase 2 cost record shape) — an estimate must never be presented as if it were a measurement.

**Provider tokenizer sourcing (verified against installed packages, not just documentation):**
| Provider | Tokenizer | Verified | Caveats |
|---|---|---|---|
| OpenAI | `tiktoken`, local | Ran live: `encoding_for_model()` resolves `o200k_base` (GPT-4o/5/o1/o3) or `cl100k_base` (GPT-4/3.5); encoded real text, got a real count | Stable, official, no extra dependency, **implemented** |
| Mistral | `mistral-common`, local | Ran live, no network/token: `MistralTokenizer.from_model('mistral-large-2411')` → real 5-token count for our test string. First attempt used a made-up model name and failed — retried with a real current model id from the library's own bundled name list and it worked | `from_model()` is deprecated (removed in 1.13.0 per the library's own warning) in favor of `from_hf_hub`/`from_file`, but still functional and still offline for now; only recognizes a fixed bundled list of model names (real, current ones — `mistral-large-2411`, `ministral-8b-2410`, etc. — not stale), **implemented** |
| Google Gemini | `google.genai.local_tokenizer.LocalTokenizer`, local | Ran live with no API key set: `LocalTokenizer('gemini-2.0-flash').count_tokens(...)` → real result, after one-time download of a SentencePiece model file (not a per-call API request) | Needs the `sentencepiece` package (not installed by `google-genai` by default); Google marks it **Experimental**; it approximates real Gemini model names through the open-weight **Gemma** tokenizer, not Gemini's literal production tokenizer — smaller-degree version of the same caveat as "tiktoken undercounts Claude." Not to be confused with `google.genai.Tokens`, the top-level class, which does require a live API client — the local, credential-free tokenizer lives one level deeper, at `google.genai.local_tokenizer`, and isn't exported from the package's top-level namespace. **Implemented.** |
| Meta Llama 3+ | none available without a gate | Checked directly (not just documentation): Meta's tokenizer `.model` file requires accepting Meta's license on their site, or a gated HuggingFace repo, before it can be downloaded at all — not `pip install`-and-go. This claim was wrong in the first draft of this table ("openly published, no barriers") until checked directly; corrected before shipping rather than after. | **Deferred**, same bucket as Anthropic — not a live per-call credential, but still an access gate this project has no story for. |
| Anthropic | none available locally | Confirmed: no open tokenizer exists; only `/v1/messages/count_tokens`, a live API call requiring an Anthropic API key | **Deferred.** The only provider needing a credential just to count tokens, and this project has no secrets-management story yet. Revisit once one exists — see `rules.md`. |

**Verified real wire format** (captured with `httpx` event hooks against the running Specialist, not assumed): the JSON-RPC request body is `{"method":"SendMessage","params":{"message":{...}},"id":...,"jsonrpc":"2.0"}`; the response is `{"result":{"task":{...}},"id":...,"jsonrpc":"2.0"}`, both in protobuf's default camelCase JSON mapping (`messageId`, `artifactId`, not the snake_case this project's own logs use). Cost estimation parses these by unwrapping the JSON-RPC envelope (`params.message` / `result.task`) and re-parsing the inner object with `a2a-sdk`'s own protobuf types via `ParseDict`, rather than hand-indexing raw dict keys — kept consistent with the rest of this project's "trust the SDK's types" approach. Parsing is best-effort: any failure here must never break request forwarding, since metering is Phase 2's job, not gatekeeping (that's Phase 3).

## Phase 3 design — budget enforcement (not implemented yet)

### The core problem: a task's own cost is never knowable before forwarding it

Phase 2 only produces a full cost estimate *after* the Specialist responds — the output side needs the response text, which doesn't exist until the request has already been sent. Our Specialist is also non-streaming and single-shot (one request, one atomic response), so there is no midpoint in the exchange where the gateway could intervene even if it wanted to. **There is no point in this architecture where the gateway can know a task's true cost before deciding whether to forward it.**

The resolution: the gateway never rejects a task for what *it* will cost — that's structurally unknowable in advance. It rejects a task because *prior* tasks already exhausted the budget this one would draw from. The running spend total is built entirely from Phase 2's own already-computed estimates, so it's always fully known before the next request arrives. One pre-flight tightening on top: the input side of the *current* request alone is knowable before sending (tokenize it the same way Phase 2 already does), so `check_budget()` rejects if `current_spend + input_only_estimate` would already exceed the ceiling — catching an obviously oversized request immediately, rather than only ever blocking the *next* one after this one already overspent.

**Accepted limitation, not worked around:** a single task can still overshoot the budget if its response costs far more than its input suggested — nothing can prevent that, since the overspend is only knowable after the response has already been delivered to the Coordinator. The only thing that could close this gap is a self-limiting Specialist (a Tier-1-style responsibility on the callee's side), not something a network-level proxy can impose from outside. Killing an in-flight request to force the cap would be a bigger integrity compromise than anything else accepted in this project — rejected for the same reason Phase 1 didn't fake a task on the Specialist's behalf.

**A second gap worth stating plainly, not fixing now:** an agent with no provider config and no self-report (Tier 3, generic fallback only) never produces a dollar figure — only a token count, with `rate_source: "not_configured"`. There is nothing to accumulate a dollar budget against for such an agent, so **an agent with no cost signal currently has no spending limit at all.** This is a direct consequence of Phase 2's own honesty rule (never fabricate a cost figure), carried forward rather than patched over with a fake number just to make enforcement "work" for that case.

### What rejection looks like on the wire — verified against the real client, not assumed

Ran both real candidates through `coordinator.delegate_task()` directly:
- A **JSON-RPC error response** (`{"jsonrpc":"2.0","id":...,"error":{"code":...,"message":...}}`, HTTP 200 per JSON-RPC 2.0 convention) → the client raises `a2a.client.errors.A2AClientError` with the message intact.
- A **plain HTTP 429** (no JSON-RPC envelope) → *also* raises the same `A2AClientError` — the SDK's HTTP layer already wraps `httpx.HTTPStatusError` the same way.

Both are equally safe from a "will it crash" standpoint. Chose the JSON-RPC error response anyway: it stays inside the protocol already being spoken, carries a structured `code`/`message` any A2A-aware client can parse (not just ours), and separates "the gateway's budget policy declined this" from "there's a network/rate-limiting problem" (what a bare 429 usually signals). Error code `-32010` — checked against `a2a-sdk`'s own reserved range (`-32001` through `-32009`, confirmed directly against `a2a/utils/errors.py`) to avoid colliding with the SDK's own error types; documented as this project's own code, not a JSON-RPC or A2A spec-defined one.

Considered and rejected: fabricating a synthetic `Task` with `TASK_STATE_FAILED` (reusing Phase 0's already-proven failure path). Rejected because it means the gateway inventing a task_id and pretending a task existed when the Specialist never saw the request — a bigger honesty compromise than the Phase 1 URL-rewrite, for no benefit once the JSON-RPC error path was confirmed clean.

**A real gap this surfaced, not a hypothetical one:** `coordinator.py`'s `delegate_task()` had no handling at all around the `client.send_message()` loop — confirmed live that a rejection's `A2AClientError` would propagate uncaught and crash `run()`/`main()`. Phase 3 is a Coordinator-side change as much as a gateway-side one.

### Scope: per-agent-name, not per-task-chain

Budget ceilings are keyed by Agent Card name — the same key `provider_config.json`/`rate_table.json` already use, so no new identity concept is introduced. Per-task-chain (`context_id`) budgets are a real feature but nothing in this project has needed one yet; per `rules.md`'s no-speculative-code rule, that's added when a real scenario demands it, not preemptively.

### State: in-memory, resets on restart

The running per-agent spend total lives in memory in the gateway process, fed by Phase 2's own cost estimates as they're computed. Not persisted — a gateway restart silently zeroes every agent's spend. Same category of limitation as Phase 1's "Agent Card fetched once at startup," deferred to whenever Phase 4 brings real storage.

### Two more known limitations — flags for later, not bugs now

- **Budget-check failures fail open, silently.** `_check_budget_before_forwarding()` wraps its entire body in `try/except Exception: return None` — deliberately, per the same "estimation/enforcement must never become a new failure mode" principle used everywhere else in Phase 2/3. But this means a genuine bug (a malformed `budget_config.json`, a tokenizer crash, a logic error) is indistinguishable from "no budget configured for this agent" — nothing is logged when the *check itself* throws, only when it runs cleanly and decides to reject. Fail-open is the right call for a toy proxy that must never block traffic due to its own bug, but a production version would want to at least log "budget check errored, allowing by default" as its own event, distinct from a normal unlimited-agent decision, so the two aren't silently conflated.
- **Pre-flight check and spend-recording race under concurrent requests.** `check_budget()` reads `_spend_by_agent[agent_name]` before forwarding; `record_spend()` writes to it only after the full round trip to the Specialist completes. Between those two points, `forward()` is `await`ing the actual HTTP call — and Python's asyncio can interleave other coroutines during any `await`. Two concurrent `SendMessage` requests to the *same* agent can both read the same pre-request spend total, both pass their pre-flight check, and both proceed, even if their combined cost would exceed the budget once both are eventually recorded. This compounds the already-accepted "a single task can overshoot" limitation above into "multiple concurrent tasks can each independently overshoot, and the check can't see any of the others' in-flight requests." Not exercised by this project's tests (they run one delegation at a time), and not fixed now — a real fix needs either a per-agent lock around the check-forward-record sequence, or a compare-and-set on the spend total, neither of which this project has needed yet.

**Why hand-rolled instead of `litellm.token_counter()`:** `litellm` already implements roughly this same provider-detection-and-fallback matrix and is actively maintained, but it's itself a full competing LLM-gateway framework — a heavy, opinionated dependency to pull in for one function, and a poor philosophical fit for a project whose whole premise is understanding exactly what's happening at each hop rather than trusting another black box (the same reasoning that justified hand-building the Phase 0 toy agents instead of using a higher-level framework). The trade-off this creates — this project now owns keeping model→encoding mappings current — is recorded in `rules.md`.

## Phase 4 design — structured tracing (not implemented yet)

Every JSONL record since Phase 0 already shares one uniform shape (`timestamp`, `task_id`, `event_type`, `actor`, `payload`) written independently by three separate processes (Coordinator, Specialist, Gateway) into their own `logs/run_*.jsonl` file per run, correlated only by `task_id`. Phase 4's job is making that queryable instead of grepped by hand — three real design forks, each verified/reasoned through before code, not picked implicitly while implementing.

### Schema: one `events` table, not several

The payload shape is genuinely heterogeneous per `event_type` (a protobuf-derived `Task`, a `CostRecord`, a `BudgetDecision`, a raw Agent Card dict), and **every phase so far has introduced a new `event_type`** — Phase 2 added `cost_estimate`, Phase 3 added `budget_rejection`/`task_rejected`. A normalized multi-table schema (`tasks`, `cost_estimates`, `budget_decisions`, ...) would need a migration every single phase — the wrong shape for a project whose whole nature has been "add new observability, don't touch what works" (confirmed as real evidence during design review, not just a hunch). A single `events` table with `payload` stored as JSON text absorbs new event types with zero schema changes, and reconstructing a task's full cross-process story becomes `SELECT * FROM events WHERE task_id = ? ORDER BY timestamp` — no joins needed, which fits "inspect a failed task chain step-by-step" more directly than normalization would have.

Verified before committing to this, not assumed: `json_extract()` and the native `->`/`->>` JSON operators (SQLite 3.38+) both work against a JSON-text payload column — checked independently in two different environments (Windows, SQLite 3.53.2; Linux, SQLite 3.45.1), same result both times. Anything that needs to reach into a specific payload field (e.g. summing `estimated_cost_usd` across a run) can do so directly; no second normalized table required for that either.

### The correlation-id gap: rejected requests have no shared identifier at all

Tracing by `task_id` works cleanly for any task that was actually created — including one that later failed (`TASK_STATE_FAILED`). It does **not** work for a budget-rejected request (see Phase 3 notes above): that request is declined before the Specialist ever creates a task, so it never gets a `task_id` — `budget_rejection` on the gateway side and `task_rejected`/`task_request` on the coordinator side all log `task_id: ""`, with no other identifier tying the two processes' records of the same attempt together. The JSON-RPC `id` that *does* correlate a request/response at the wire level is generated inside `a2a-sdk`'s transport layer, one level below where `coordinator.py` logs the `SendMessageRequest` object — our own logging never sees it today.

Two options, not a forced answer:
1. Capture and propagate the JSON-RPC id as a real `correlation_id`, closing the gap fully.
2. Scope Phase 4 to `task_id`-bearing chains only; a rejected attempt stays a standalone, ungrouped row on whichever side logged it.

**Chose (2)**, and for a stronger reason than "cheaper for now": correlating rejected attempts by anything weaker than a real shared id — e.g. timestamp proximity — collides directly with the concurrency race already accepted in the Phase 3 notes above. Two *concurrent* rejected requests to the same agent would be genuinely indistinguishable by timing, which is exactly the condition under which a heuristic correlation would matter most and mislead most. Leaving rejected attempts as honest, standalone rows is the more correct choice, not merely the simpler one. Revisit with a real `correlation_id` if rejected-attempt tracing becomes a real need.

### Ingestion: JSONL stays canonical; SQLite is a derived, on-demand, full-rebuild view

Two real options: (a) tail the existing JSONL files into SQLite, changing nothing about how the three processes log; or (b) have `log_event()` write directly to SQLite going forward, retiring JSONL. Chose (a): JSONL has been the plain-text, always-`cat`-able ground truth this project has leaned on for verification at every phase so far, and (b) turns three independent processes into concurrent writers of one SQLite file for no clear benefit at this project's scale (WAL mode was verified to handle it, but it's a new failure surface bought for nothing this project currently needs).

Ingestion is a small on-demand script (`tracing/build_trace_db.py`), not a continuously running daemon — this project hasn't needed persistent background infrastructure yet, and a daemon would need to solve file-tailing, partial-write handling, and restart-offset tracking for no immediate benefit. Explicit scope limit: it's meant to run against JSONL files from processes that have already stopped, not one being actively appended to — same "documented, not fixed" treatment as every other limitation here.

**Idempotency, decided before writing the script rather than discovered after**: every phase's demos and test runs have already accumulated many `logs/run_*.jsonl` files. Running the ingestion script **drops and fully rebuilds** `logs/trace.db` from every `logs/run_*.jsonl` file present, every time — no dedup key, no incremental-append tracking, no unique constraint on `(source_file, line_number)`. Chosen for the same reason Phase 3's budget state resets on restart rather than persisting: nothing in this project has needed durability across runs yet, and at this log volume a full re-parse costs nothing. Revisit if the JSONL volume or a real need for incremental ingestion ever makes a full rebuild actually expensive.

## Phase 5 design — test harness (not implemented yet)

README describes this phase as "simulate retries, streaming responses, and failed sub-tasks, and verify metering stays accurate under those conditions." Unlike every phase before it, Phase 5 doesn't add one clean new capability to a known-good path — its job is deliberately exercising failure modes, which means the scope of *what's actually being tested* needs deciding before code, not picked implicitly while writing tests.

### Streaming: excluded, not attempted

Turning streaming on isn't a test-harness-sized change — it would break assumptions baked into three already-shipped phases at once: Phase 1's `forward()` fully buffers both request and response bodies (already flagged in that phase's known limitations as a real gap if a future Specialist enables streaming); Phase 2's `_extract_terminal_task()` says outright in its own docstring that it's "non-streaming only... our Specialist always returns the complete final Task in one response, never a partial update"; Phase 3's budget check leans entirely on "one request, one atomic response" for its pre-flight/post-hoc timing to make sense at all. Simulating streaming without first building real streaming support across all three would test nothing real. **Excluded from Phase 5.** Real streaming support, if ever needed, is its own phase with its own design pass — not a Phase 5 side effect.

### Retries: no new retry logic exists to test — reframed as a concurrency test instead

Checked before assuming: `a2a.client.ClientConfig` has no retry-related field at all, and neither `coordinator.py` nor `gateway/proxy.py` configures a retrying `httpx` transport. **There is no automatic retry anywhere in this system.** Since `new_task_from_user_message()` mints a fresh UUID on every call, a caller-initiated retry is structurally just another independent task — nothing to deduplicate, no double-counting risk, because the same `task_id` can never be produced twice. Building retry machinery nobody asked for would violate `rules.md`'s no-speculative-code rule for no real benefit.

Instead: `arch.md`'s own Phase 3 notes already **claim** a real concurrency race (pre-flight check reads `_spend_by_agent` before forwarding; `record_spend()` writes it only after the full round trip) but it has never actually been exercised — a documented belief, not a verified fact. Concurrent retries against an agent sitting at its budget boundary is exactly the scenario that would prove or disprove it, and doing so converts a claim sitting unverified in a doc into either a reproduced, confirmed finding or a correction — a better use of this phase than inventing synthetic retry scenarios that don't correspond to anything the architecture actually does.

**Making the race reliably reproducible, not just "fire two requests and hope":** the race only actually manifests during the `await` on the HTTP call to the Specialist — and the toy Specialist's `.upper()` responds almost instantly, so two concurrent requests might not interleave at the right point often enough for a naive test to be trustworthy. A flaky race test that sometimes catches the bug and sometimes doesn't is worse than no test — it looks like coverage without being trustworthy coverage. Fix: add an artificial delay to `SpecialistAgent.invoke()`, gated behind a test-only env flag (same opt-in pattern as `SPECIALIST_SIMULATE_COST_REPORT` — off by default, zero effect on Phase 0–4 behavior), long enough to deterministically widen the `await` window so concurrent requests reliably interleave inside it.

**What the test asserts, and why that has to be unmistakable:** the test fires N concurrent delegations against an agent budgeted for roughly one, and confirms more than one gets allowed through — i.e. it asserts the **current, known-bad** behavior as today's expected outcome. That's correct and valuable (it's exactly how "documented belief" becomes "reproducible fact"), but if it's not clearly labeled, a future fix to the race (the per-agent lock or compare-and-set already named in the Phase 3 notes as the real fix) would make this test start failing and look like a regression instead of the improvement it actually is. The test's docstring says so explicitly: this test currently passes by confirming the race exists; if Phase 3's concurrency handling is ever hardened, this test's assertion is expected to flip, and that's success, not breakage.

### Failed sub-tasks: mostly already built, never kept as a real test

Phase 0 built and verified the `TASK_STATE_FAILED` path — but only with a throwaway verification script, deleted right after manual confirmation, never promoted to a permanent test. Concrete Phase 5 task: promote it to `tests/test_specialist_failure.py`. While there, also check whether Phase 2's cost estimation behaves *sensibly* on a failed task — `_extract_terminal_task()` already includes `TASK_STATE_FAILED` in its terminal-state check, so a failed task isn't silently skipped, but a failed task typically has no artifacts (empty output text), even though the Specialist may have burned real resources before failing. Two possible outcomes from checking this, treated differently: if what it produces is simply consistent with `scope_note`'s already-documented honesty (an estimate that only ever covered visible text, and a failed task has less visible text than usual — not a new limitation, just the existing one showing up differently), confirm that and move on. If it instead produces something that looks like an actual bug — a crash, a nonsensical value, a divide-by-zero — that gets fixed on its own merits, the same as the Phase 4 malformed-line finding, not filed away as "just another documented limitation" to dodge scope creep.
