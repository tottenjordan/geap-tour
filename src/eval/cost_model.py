"""Fair per-request cost model for the coordinator bake-off (token -> USD).

Puts a comparable dollar figure next to each backbone's quality number so the
Gemini-vs-Claude report can state the quality/cost tradeoff. The router's
``run_cost_efficiency_eval`` prices 5-tier *routing* and cannot price a single
coordinator turn, so this is a small, standalone, pure cost model:

  - **Gemini** (``gemini-3.6-flash``) bills per token: ``input$/tok`` and
    ``output$/tok`` directly.
  - **Claude on Vertex** bills in **GSU** (Generative Scale Units), not raw
    tokens. Per Vertex partner-model docs, at <200k context 1 input token = 1
    GSU and 1 output token = 5 GSU; we convert GSU -> USD with a single rate.

.. warning::
   The rate constants below are DIRECTIONAL and move with published pricing.
   Verify every number against live Vertex pricing before quoting any
   stakeholder-facing figure. They are centralized here precisely so a single
   edit re-prices the whole report.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

# --------------------------------------------------------------------------- #
# Rate table — VERIFY AGAINST LIVE PRICING BEFORE QUOTING (see module warning).
# --------------------------------------------------------------------------- #
# Gemini 3.x flash-class per-token USD (illustrative flash-tier rates, USD/token
# = USD per 1M tokens / 1e6). Claude Sonnet on Vertex prices in GSU; usd_per_gsu
# is the GSU -> USD conversion (illustrative). Encoded as plain constants so the
# whole bake-off re-prices from one place.
RATES: dict[str, dict] = {
    "gemini-3.6-flash": {
        "kind": "per_token",
        # $0.15 / 1M input tokens, $0.60 / 1M output tokens (illustrative).
        "input_usd_per_token": 0.15 / 1_000_000,
        "output_usd_per_token": 0.60 / 1_000_000,
    },
    "claude-sonnet-5": {
        "kind": "gsu",
        # Vertex partner-model GSU burndown at <200k context.
        "input_gsu_per_token": 1,
        "output_gsu_per_token": 5,
        # $ per GSU (illustrative — verify against live Vertex pricing).
        "usd_per_gsu": 0.60 / 1_000_000,
    },
}


def per_request_cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float:
    """USD cost of a single request on ``model_id`` for the given token counts.

    Raises ``KeyError`` for an unknown model and ``ValueError`` for negative
    token counts.
    """
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError(f"token counts must be >= 0, got {input_tokens}/{output_tokens}")
    rate = RATES[model_id]
    if rate["kind"] == "per_token":
        return (
            input_tokens * rate["input_usd_per_token"]
            + output_tokens * rate["output_usd_per_token"]
        )
    # GSU burndown -> USD.
    gsu = (
        input_tokens * rate["input_gsu_per_token"]
        + output_tokens * rate["output_gsu_per_token"]
    )
    return gsu * rate["usd_per_gsu"]


def _tokens(usage: Mapping) -> tuple[int, int]:
    """Pull ``(input_tokens, output_tokens)`` from a usage dict; missing -> 0."""
    return int(usage.get("input_tokens", 0) or 0), int(usage.get("output_tokens", 0) or 0)


def aggregate_cost_usd(model_id: str, usages: Iterable[Mapping]) -> float:
    """Total USD across a sequence of per-request usage dicts."""
    return sum(per_request_cost_usd(model_id, *_tokens(u)) for u in usages)


def cost_summary(model_id: str, usages: Iterable[Mapping]) -> dict:
    """Total + mean per-request USD for a model over its usage records."""
    usages = list(usages)
    n = len(usages)
    total = aggregate_cost_usd(model_id, usages)
    return {
        "model": model_id,
        "n_requests": n,
        "total_usd": total,
        "mean_usd_per_request": (total / n) if n else 0.0,
    }
