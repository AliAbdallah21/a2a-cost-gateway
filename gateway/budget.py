"""Phase 3 budget enforcement — gate on the past, not the future.

The gateway cannot know a task's true cost before forwarding it: Phase 2's
whole premise is that the response side is unknowable until the Specialist
replies, and our non-streaming, single-shot architecture has no midpoint to
intervene at. So a task is never rejected for its own cost — it's rejected
because prior tasks already exhausted the budget it would draw from (see
arch.md's Phase 3 design notes). One pre-flight tightening: the input side
alone IS knowable before sending, so a request whose input, added to the
running total, would already exceed the budget gets caught immediately
rather than waiting for the next task after this one already overspent.

In-memory only, per agent name (same key as provider_config.json/rate_table.json
— see arch.md/rules.md for why this doesn't yet support per-task-chain scoping).
Resets on gateway restart — an explicit, documented limitation, same treatment
as Phase 1's Agent Card caching. Only meaningful for agents with a real $ cost
signal (Tier 1 self-report or Tier 2 provider tokenizer); an unconfigured
(Tier 3-only) agent has no dollar figure to accumulate against and so is never
budget-gated — see arch.md and gateway/cost_estimation.py.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

GATEWAY_DIR = Path(__file__).resolve().parent
BUDGET_CONFIG_PATH = Path(os.getenv("BUDGET_CONFIG_PATH", str(GATEWAY_DIR / "budget_config.json")))

_spend_by_agent: dict[str, float] = {}


@dataclass
class BudgetDecision:
    """See schema.md's Phase 3 budget rejection log event shape."""

    allowed: bool
    agent_name: str
    budget_usd: float | None
    current_spend_usd: float
    projected_spend_usd: float
    reason: str | None = None


def load_budget_config() -> dict[str, dict[str, float]]:
    """Agent name -> {budget_usd}. Missing file or unlisted agent => unlimited (never rejected)."""
    if not BUDGET_CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(BUDGET_CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {k: v for k, v in data.items() if not k.startswith("_")}


def get_current_spend(agent_name: str) -> float:
    return _spend_by_agent.get(agent_name, 0.0)


def record_spend(agent_name: str, amount_usd: float | None) -> None:
    """Add a Phase 2 cost estimate to the running total. A None amount (no $ signal
    for that task — e.g. a Tier 3 generic-fallback record) is not recorded, never
    fabricated as zero or otherwise."""
    if amount_usd is None:
        return
    _spend_by_agent[agent_name] = _spend_by_agent.get(agent_name, 0.0) + amount_usd


def check_budget(
    agent_name: str,
    input_only_cost_usd: float | None,
    budget_config: dict[str, dict[str, float]],
) -> BudgetDecision:
    """Pre-flight: would forwarding this request already be over budget, given what's
    knowable before forwarding (accumulated past spend + this request's input-only cost)?
    """
    current = get_current_spend(agent_name)
    entry = budget_config.get(agent_name)

    if entry is None:
        return BudgetDecision(
            allowed=True,
            agent_name=agent_name,
            budget_usd=None,
            current_spend_usd=current,
            projected_spend_usd=current,
        )

    budget = entry["budget_usd"]
    projected = current + (input_only_cost_usd or 0.0)

    if current >= budget:
        return BudgetDecision(
            allowed=False,
            agent_name=agent_name,
            budget_usd=budget,
            current_spend_usd=current,
            projected_spend_usd=projected,
            reason=f"budget already exhausted: ${current:.6f} spent >= ${budget:.6f} limit",
        )
    if projected > budget:
        return BudgetDecision(
            allowed=False,
            agent_name=agent_name,
            budget_usd=budget,
            current_spend_usd=current,
            projected_spend_usd=projected,
            reason=(
                f"this request's input alone would push spend to ${projected:.6f}, "
                f"over the ${budget:.6f} limit"
            ),
        )
    return BudgetDecision(
        allowed=True,
        agent_name=agent_name,
        budget_usd=budget,
        current_spend_usd=current,
        projected_spend_usd=projected,
    )
