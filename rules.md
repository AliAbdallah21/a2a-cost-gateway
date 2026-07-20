# Engineering Rules

These apply to Phase 0 now and are meant to still hold once later phases start — write Phase 0 code as if it will be read and extended, not thrown away.

## Coding conventions
- **Python style**: follow PEP 8. 4-space indentation, `snake_case` for functions/variables, `PascalCase` for classes.
- **Type hints**: required on all function signatures (params and return type) — this project cares about correctness, and the A2A SDK's own types should be used/re-exported rather than re-declared.
- **Docstrings**: every module and every public function gets a one-to-a-few-line docstring stating what it does and, where non-obvious, why. No docstrings that just restate the function name.
- **No dead code / no speculative code**: don't write functions, parameters, or branches for Phase 1+ behavior "while we're in here." If Phase 0 doesn't need it, it doesn't go in.
- **Environment**: this project uses the `agent-com` Anaconda environment. Any new dependency gets added to `requirements.txt` and installed into `agent-com`, not the base environment.

## Scope discipline
- **Do not implement metering, budget enforcement, or proxy/interception logic until Phase 0 is confirmed working and reviewed.** This is the single most important rule for this stage of the project — see `README.md`'s "Build order — do NOT skip ahead."
- `gateway/` stays empty through Phase 0. If you find yourself wanting to add a file there, stop — that's a Phase 1 signal, not a Phase 0 task.
- If a Phase 0 task naturally suggests future work (e.g. "we'll need to estimate tokens here eventually"), note it as a comment or in `schema.md`'s future-needs section — do not build it now.

## Logging conventions
- **Console**: every observable event in the exchange (Agent Card fetch, each task state transition, request/response payloads) is printed as it happens, human-readable, in the order it occurs. This is the primary deliverable of Phase 0 — if it's not visible on the console, Phase 0 hasn't proven anything.
- **`logs/`**: the same events, written as raw JSON-lines (one JSON object per line) to a per-run log file, per the field structure in `schema.md`. Console output is for humans watching in real time; the log file is for replay/inspection afterward — keep them consistent with each other (same events, same data), not divergent formats.
- Do not truncate, summarize, or pretty-print-then-lose fields from a payload before logging it — log the real payload.
- Log file naming: timestamped per run (e.g. `logs/run_<ISO8601-ish-timestamp>.jsonl`) so repeated runs don't clobber each other.

## Testing expectation
- Even Phase 0 needs a basic automated test confirming the delegation completes successfully end-to-end (Coordinator delegates → Specialist responds → Coordinator sees `completed`). It doesn't need to be exhaustive — one smoke test is the Phase 0 bar. This lives in `tests/` per the folder structure in `README.md`, even though `tests/` is otherwise reserved for the Phase 5 harness.
- The test should fail loudly if the exchange doesn't reach `completed`, rather than silently passing on a partial run.

## Secrets and configuration
- No API keys, tokens, or secrets committed to the repo, ever — Phase 0 shouldn't need any (it's local-only, no LLM calls, no external services), but the rule holds regardless.
- Any configuration that could vary by environment (ports, URLs) goes in a `.env` file (gitignored) with a checked-in `.env.example` showing the expected keys, not hardcoded magic numbers scattered through the code.

## Dependency maintenance cost (Phase 2+)
- Phase 2's cost estimation hand-rolls per-provider tokenizer integrations (`tiktoken`, `mistral-common`, `google-genai`'s local tokenizer) instead of adopting a library like `litellm` that already maintains a model-name → tokenizer/encoding mapping across providers. That was a deliberate call — see `arch.md`'s Phase 2 notes — made because pulling in a whole competing LLM-gateway framework as a dependency for one function was a worse fit than the small amount of code this needs.
- The trade-off: **this project now owns keeping those model→encoding mappings current** as providers ship new models, rather than that being someone else's maintained problem. This is an ongoing cost, not a one-time one — flagged here explicitly so it's a known trade-off, not a surprise found six months from now when a new model returns wrong or missing token counts.
