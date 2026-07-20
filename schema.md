# Data Structures — Phase 0 / Phase 2 design

These shapes follow A2A Protocol Specification 1.0 as implemented by the official `a2a-sdk`. Field lists below are the ones we actually populate for our two toy agents — the spec allows more optional fields than are listed here.

## Agent Card

Each A2A agent publishes an Agent Card — a JSON document describing what it is and how to reach it, served at a well-known discovery endpoint. For Phase 0:

```json
{
  "name": "Specialist",
  "description": "Toy A2A agent that completes a delegated sub-task for Phase 0 testing.",
  "version": "0.1.0",
  "url": "http://localhost:<specialist_port>/",
  "capabilities": {
    "streaming": false,
    "pushNotifications": false
  },
  "defaultInputModes": ["text/plain"],
  "defaultOutputModes": ["text/plain"],
  "skills": [
    {
      "id": "toy-task",
      "name": "Toy Task",
      "description": "Performs a trivial, deterministic transformation on the input for testing purposes."
    }
  ]
}
```

- The **Coordinator** does not need to publish its own Agent Card for Phase 0 (it only acts as a client), but its module should log the Specialist's card exactly as received — not a hand-summarized version — so we see the real discovery payload.
- `authentication` is omitted for Phase 0 (no auth) — noted so it isn't mistaken for an oversight later.

## Task request / response payload

A2A tasks are exchanged as JSON-RPC-style messages over HTTP. What we log for Phase 0 is the **actual wire payload**, not a paraphrase. Shape (illustrative — exact field names come from `a2a-sdk`'s message/types, log what the SDK actually produces):

**Request (Coordinator → Specialist):**
```json
{
  "task_id": "<uuid>",
  "message": {
    "role": "user",
    "parts": [
      { "type": "text", "text": "<toy task payload>" }
    ]
  }
}
```

**Response / status updates (Specialist → Coordinator)** — one per state transition:
```json
{
  "task_id": "<uuid>",
  "status": {
    "state": "submitted",
    "timestamp": "<ISO8601>"
  }
}
```
```json
{
  "task_id": "<uuid>",
  "status": {
    "state": "working",
    "timestamp": "<ISO8601>"
  }
}
```
```json
{
  "task_id": "<uuid>",
  "status": { "state": "completed", "timestamp": "<ISO8601>" },
  "artifacts": [
    { "type": "text", "text": "<toy result payload>" }
  ]
}
```

Valid `state` values per the A2A task lifecycle include (at minimum, for Phase 0's purposes): `submitted`, `working`, `completed`, `failed`. Other spec-defined states (`input-required`, `canceled`, `rejected`, `auth-required`, `unknown`) exist but are not expected to occur in the Phase 0 happy path — if one shows up, log it as-is rather than coercing it into one of the four above.

## What we log for Phase 0

Two destinations, same content:
1. **Console** — human-readable, in real time, as each event happens.
2. **`logs/`** — the same events, raw, one JSON object per line (JSON-lines), written to a file per run so a run can be replayed/inspected later.

Minimum fields per logged event:
| Field | Meaning |
|---|---|
| `timestamp` | ISO 8601, when we observed the event (not just the payload's own timestamp) |
| `task_id` | the A2A task id this event belongs to |
| `event_type` | one of: `agent_card_fetch`, `task_state_transition`, `task_request`, `task_response` |
| `actor` | which of our two agents produced/received this event (`coordinator` / `specialist`) |
| `payload` | the full raw JSON for this event (Agent Card, request body, status update, or final result) — unmodified |

No summarization or truncation of `payload` in Phase 0 — the entire point of this phase is seeing the real data, so partial logs would defeat it.

## Phase 2 — provider mapping config (implemented)

The gateway cannot infer which LLM provider/model backs a given Specialist from the A2A envelope — nothing in the Agent Card or task payload declares it. Tier 2 cost estimation (see `arch.md`'s Phase 2 notes) needs this told to it explicitly by whoever deploys the gateway. Proposed shape, a small JSON sidecar (not `.env` — this is structural mapping data, not per-environment runtime config, and it's not a secret):

`gateway/provider_config.json`
```json
{
  "Specialist": {
    "provider": "openai",
    "model": "gpt-4o-mini"
  }
}
```
- Keyed by the Agent Card's `name` field — the same value already visible in every `agent_card_fetch` log event, so there's no new identity concept to introduce.
- `provider` is one of the set the gateway knows how to tokenize for **locally, with no access gate**: `openai`, `mistral`, `google`. (`anthropic` and `llama` deliberately excluded for now — see `arch.md`'s Phase 2 notes: Anthropic needs a live API credential just to count tokens, and Llama's tokenizer requires accepting Meta's license/a gated download before it's even available locally. Neither has a story in this project yet — see `rules.md`.)
- `model` is passed straight through to the relevant tokenizer library (e.g. `tiktoken.encoding_for_model(model)`).
- File is optional. Missing file, or an agent name not present in it, means that Specialist falls straight to Tier 3 (generic fallback) — no crash, no required config for the system to keep working end to end.
- Loaded once at gateway startup, same lifecycle as the Agent Card fetch/rewrite already done in `fetch_rewritten_agent_card()`.

## Phase 2 — self-reported cost metadata convention (implemented)

A cooperating Specialist can optionally attach real usage data to its task, which the gateway always prefers over any estimate when present (Tier 1). Proposed convention — a `cost` key inside the existing `metadata` field already present on `TaskStatusUpdateEvent`/`Task` (a real, unused extensibility point in the A2A spec, not a new field we're inventing):
```json
{
  "metadata": {
    "cost": {
      "provider": "demo",
      "model": "demo-model",
      "input_tokens": 12,
      "output_tokens": 4,
      "cost_usd": 0.0003
    }
  }
}
```
All sub-fields required if `cost` is present at all — a partial self-report (e.g. tokens but no `cost_usd`) is treated as absent and falls through to Tier 2/3 estimation, rather than the gateway trying to guess the missing pieces.

## Phase 2 — cost record log (implemented)

Extends the existing `log_event` shape (same two destinations: console + this run's JSONL file), as its own `event_type`, one record per completed task:
| Field | Meaning |
|---|---|
| `task_id` | same task id as the rest of that task's events |
| `estimation_method` | `self_reported` \| `provider_tokenizer:<provider>:<model>` \| `generic_fallback` — which tier produced this record |
| `input_tokens` / `output_tokens` | counted or self-reported, per `estimation_method` |
| `estimated_cost_usd` | `null` when only a token count is available and no $/token rate is configured for that provider/model |
| `rate_source` | where the $/token rate came from (a rate table this project maintains — see `rules.md`'s maintenance note; neither `tiktoken` nor any tokenizer library returns price) |
| `scope_note` | fixed string reminding readers this covers only the visible A2A message + artifact text, not any internal LLM calls the Specialist made to produce it |

Phase 0's `task_id`-keyed, timestamped, JSON-lines event log (see above) is intentionally shaped so this slots in as one more `event_type` without a redesign.

## Phase 3 — budget config (implemented)

The gateway needs a per-agent spend ceiling told to it explicitly, same pattern as `provider_config.json`. Proposed shape:

`gateway/budget_config.json`
```json
{
  "Specialist": {
    "budget_usd": 1.0
  }
}
```
- Keyed by Agent Card `name`, same identity already used by `provider_config.json`/`rate_table.json` — see `arch.md`'s Phase 3 notes on why this project isn't introducing per-`context_id` scoping yet.
- File is optional; an agent absent from it has **no budget enforced at all** (unlimited), same missing-config-is-safe default every other Phase 2/3 config file uses.
- Path overridable via a `BUDGET_CONFIG_PATH` env var (falls back to the file above) — the same env-var-for-per-run-config pattern already used for `SPECIALIST_HOST`/`GATEWAY_PORT`/`COORDINATOR_TARGET_URL`, needed so tests can exercise a real rejection deterministically without a tiny value living in the checked-in default.

## Phase 3 — budget rejection log event (implemented)

Logged via the existing `log_event` mechanism (console + JSONL), `event_type: "budget_rejection"`, `actor: "gateway"`, emitted *instead of* forwarding — the Specialist never sees a rejected request, so there's no real `task_id` to key this by (`task_id: ""`, same convention `agent_card_fetch`/`task_request` already use before a task exists):
| Field | Meaning |
|---|---|
| `agent_name` | which configured agent's budget was checked |
| `budget_usd` | the configured ceiling |
| `current_spend_usd` | accumulated spend for this agent before this request, per Phase 2's own logged estimates |
| `projected_spend_usd` | `current_spend_usd` + this request's pre-flight input-only cost estimate (the only part of *this* request's cost that's knowable before forwarding) |
| `reason` | human-readable — either "budget already exhausted" or "this request's input alone would exceed it" |

## Phase 3 — JSON-RPC rejection response (implemented)

What the Coordinator actually receives when rejected — a standard JSON-RPC 2.0 error, `id` matching the original request's `id` for correlation:
```json
{
  "jsonrpc": "2.0",
  "id": "<same id as the request>",
  "error": {
    "code": -32010,
    "message": "Budget exceeded: <reason>"
  }
}
```
`-32010` is this project's own code (outside `a2a-sdk`'s reserved `-32001`–`-32009` range, confirmed against `a2a/utils/errors.py` — see `arch.md`), not a JSON-RPC or A2A spec-defined one.

## Phase 4 — trace store schema (not implemented yet, design only)

One table, deliberately not normalized — see `arch.md`'s Phase 4 design notes for why (every phase so far has added a new `event_type`; a multi-table schema would need a migration every time).

`logs/trace.db`, table `events`:
```sql
CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    source_file TEXT NOT NULL,   -- which logs/run_*.jsonl this row came from, for debugging ingestion itself
    timestamp TEXT NOT NULL,     -- ISO 8601, same value already in the JSONL record
    task_id TEXT NOT NULL,       -- "" for events logged before/without a real task (see arch.md's correlation-id gap)
    event_type TEXT NOT NULL,    -- agent_card_fetch | task_request | task_state_transition | task_response
                                  -- | cost_estimate | budget_rejection | task_rejected
    actor TEXT NOT NULL,         -- coordinator | specialist | gateway
    payload TEXT NOT NULL        -- the JSONL record's payload, stored as JSON text (json_extract()/->>'able)
);

CREATE INDEX idx_events_task_id ON events(task_id);
CREATE INDEX idx_events_event_type ON events(event_type);
```
A one-to-one mapping from each existing JSONL line to one row — no new fields invented, no data dropped or reshaped, `payload` stored exactly as the JSONL file already has it (a JSON string) so `json_extract(payload, '$.estimated_cost_usd')` or `payload ->> '$.estimated_cost_usd'` work directly (verified against SQLite 3.53.2 and 3.45.1 — see `arch.md`).

**Reconstructing a task's full cross-process story** (the concrete point of this whole phase):
```sql
SELECT timestamp, actor, event_type, payload
FROM events
WHERE task_id = ?
ORDER BY timestamp;
```

**Rebuild behavior**: every run of `tracing/build_trace_db.py` drops and recreates the `events` table from scratch, reading every `logs/run_*.jsonl` file present at that moment. No dedup key, no incremental-append tracking — see `arch.md`'s Phase 4 notes for why that's the deliberate choice here, not an oversight.
