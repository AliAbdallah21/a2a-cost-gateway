"""Phase 2 cost estimation — tiered: self-reported > provider tokenizer > generic fallback.

The gateway only ever sees the A2A envelope (the visible input message + output
artifact text) — never what the Specialist did internally to produce it. Every
CostRecord this module produces is labeled with which tier produced it via
`estimation_method`, and carries a fixed `scope_note` — an estimate must never
be mistaken for a measurement (see arch.md's Phase 2 design notes).

Best-effort throughout: any failure here (unknown provider, tokenizer error,
malformed metadata) must never raise into the caller — it degrades to the next
tier instead. Cost estimation is Phase 2's observability job, not gatekeeping
(that's Phase 3); it must never be able to break request forwarding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GATEWAY_DIR = Path(__file__).resolve().parent
PROVIDER_CONFIG_PATH = GATEWAY_DIR / "provider_config.json"
RATE_TABLE_PATH = GATEWAY_DIR / "rate_table.json"

GENERIC_FALLBACK_CHARS_PER_TOKEN = 4

SCOPE_NOTE = (
    "Covers only the visible A2A input message and output artifact text — "
    "not any internal LLM calls the Specialist made to produce it."
)

REQUIRED_SELF_REPORT_FIELDS = {"provider", "model", "input_tokens", "output_tokens", "cost_usd"}


@dataclass
class CostRecord:
    """See schema.md's Phase 2 cost record shape."""

    task_id: str
    estimation_method: str
    input_tokens: float | None
    output_tokens: float | None
    estimated_cost_usd: float | None
    rate_source: str | None
    scope_note: str = SCOPE_NOTE


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def load_provider_config() -> dict[str, dict[str, str]]:
    """Agent Card name -> {provider, model}. Missing file or unlisted agent => Tier 3 only."""
    return {k: v for k, v in _load_json(PROVIDER_CONFIG_PATH).items() if not k.startswith("_")}


def load_rate_table() -> dict[str, dict[str, float]]:
    """'<provider>:<model>' -> $/1M-token rates. Missing entry => estimated_cost_usd stays None."""
    return {k: v for k, v in _load_json(RATE_TABLE_PATH).items() if not k.startswith("_")}


def _cost_from_rate(rate: dict[str, float] | None, input_tokens: float, output_tokens: float) -> float | None:
    if rate is None:
        return None
    return (input_tokens / 1_000_000) * rate["input_per_million_usd"] + (
        output_tokens / 1_000_000
    ) * rate["output_per_million_usd"]


# ---- Tier 1: self-reported (ground truth, when a cooperating Specialist provides it) ----


def extract_self_reported_cost(task_metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    """Pull a *complete* self-reported cost block from Task.metadata. Partial blocks are treated as absent."""
    if not task_metadata:
        return None
    cost = task_metadata.get("cost")
    if not isinstance(cost, dict) or not REQUIRED_SELF_REPORT_FIELDS.issubset(cost.keys()):
        return None
    return cost


# ---- Tier 2: provider-specific tokenizers — see arch.md's Phase 2 table for what's verified ----


def _count_tokens_openai(text: str, model: str) -> int:
    import tiktoken

    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding("o200k_base")
    return len(encoding.encode(text))


def _count_tokens_mistral(text: str, model: str) -> int:
    from mistral_common.tokens.tokenizers.mistral import MistralTokenizer

    tokenizer = MistralTokenizer.from_model(model)
    return len(tokenizer.instruct_tokenizer.tokenizer.encode(text, bos=False, eos=False))


def _count_tokens_google(text: str, model: str) -> int:
    from google.genai import types as genai_types
    from google.genai.local_tokenizer import LocalTokenizer

    tokenizer = LocalTokenizer(model)
    result = tokenizer.count_tokens(
        contents=[genai_types.Content(role="user", parts=[genai_types.Part(text=text)])]
    )
    return result.total_tokens


# Deliberately excludes anthropic (needs a live API credential) and llama (needs
# license-gated access) — see arch.md's Phase 2 table for why both are deferred.
_PROVIDER_TOKENIZERS = {
    "openai": _count_tokens_openai,
    "mistral": _count_tokens_mistral,
    "google": _count_tokens_google,
}


def _count_tokens_provider(provider: str, model: str, text: str) -> int | None:
    """None if the provider is unsupported or the tokenizer call fails — caller falls back to Tier 3."""
    counter = _PROVIDER_TOKENIZERS.get(provider)
    if counter is None:
        return None
    try:
        return counter(text, model)
    except Exception:
        return None


# ---- Phase 3: pre-flight input-only estimate, for the budget check before forwarding ----
# Tier 1 (self-report) isn't available pre-flight — it only ever comes from the response's
# metadata. Tier 3 (generic fallback) has no $ rate, so it can't produce a dollar figure to
# check a budget against. So this is Tier-2-only: if the agent isn't configured with a
# provider, or the tokenizer/rate lookup fails, there's simply no pre-flight cost signal —
# see arch.md's Phase 3 notes on why an unconfigured agent has no budget enforced at all.


def estimate_input_only_cost(
    agent_name: str | None,
    input_text: str,
    provider_config: dict[str, dict[str, str]],
    rate_table: dict[str, dict[str, float]],
) -> float | None:
    """None if not computable (unconfigured agent, unsupported provider, no rate) — never fabricated."""
    entry = provider_config.get(agent_name) if agent_name else None
    if not entry:
        return None
    provider = entry.get("provider", "")
    model = entry.get("model", "")
    input_tokens = _count_tokens_provider(provider, model, input_text)
    if input_tokens is None:
        return None
    rate = rate_table.get(f"{provider}:{model}")
    if rate is None:
        return None
    return (input_tokens / 1_000_000) * rate["input_per_million_usd"]


# ---- Tier 3: generic fallback — always available, always the least trustworthy ----


def _count_tokens_generic(text: str) -> int:
    """Rough, provider-agnostic approximation. Never claims precision it can't have."""
    if not text:
        return 0
    return max(1, len(text) // GENERIC_FALLBACK_CHARS_PER_TOKEN)


# ---- Orchestration ----


def estimate_cost(
    task_id: str,
    input_text: str,
    output_text: str,
    task_metadata: dict[str, Any] | None,
    agent_name: str | None,
    provider_config: dict[str, dict[str, str]],
    rate_table: dict[str, dict[str, float]],
) -> CostRecord:
    """Tier 1 (self-reported) > Tier 2 (provider tokenizer, if agent_name is configured) > Tier 3 (generic)."""
    self_reported = extract_self_reported_cost(task_metadata)
    if self_reported is not None:
        return CostRecord(
            task_id=task_id,
            estimation_method="self_reported",
            input_tokens=self_reported["input_tokens"],
            output_tokens=self_reported["output_tokens"],
            estimated_cost_usd=self_reported["cost_usd"],
            rate_source="self_reported",
        )

    entry = provider_config.get(agent_name) if agent_name else None
    if entry:
        provider = entry.get("provider", "")
        model = entry.get("model", "")
        input_tokens = _count_tokens_provider(provider, model, input_text)
        output_tokens = _count_tokens_provider(provider, model, output_text)
        if input_tokens is not None and output_tokens is not None:
            rate = rate_table.get(f"{provider}:{model}")
            cost = _cost_from_rate(rate, input_tokens, output_tokens)
            return CostRecord(
                task_id=task_id,
                estimation_method=f"provider_tokenizer:{provider}:{model}",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=cost,
                rate_source=f"rate_table:{provider}:{model}" if cost is not None else "not_configured",
            )
        # provider tokenizer unsupported or failed -> fall through to Tier 3

    return CostRecord(
        task_id=task_id,
        estimation_method="generic_fallback",
        input_tokens=_count_tokens_generic(input_text),
        output_tokens=_count_tokens_generic(output_text),
        estimated_cost_usd=None,
        rate_source="not_configured",
    )
