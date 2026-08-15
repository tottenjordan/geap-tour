"""Held-out eval prompts — reserved for grading, never used for GEPA training.

The offline eval historically graded each agent on the *same* prompts GEPA
optimized against (see :mod:`src.eval.dataset_integrity`), so scores measured
memorization rather than generalization. This manifest declares, per agent, a
subset of the eval-time evalset (`src/eval/evalsets/*`) that is **held out**: those
prompts are removed from the GEPA training evalsets (`src/agents/*/*.evalset.json`)
so the agent is never optimized on them. `tests/test_eval_dataset_integrity.py`
enforces the split (holdout ∩ train == ∅) and fails CI if a future edit
re-contaminates it.

Selection spans categories (search / policy / submit / edge / multi-step) so the
held-out slice is a representative generalization probe, not a corner. Router's
holdout is the set of complex multi-step prompts that were already eval-only.
"""

from __future__ import annotations

from src.eval import dataset_integrity as di

# Eval-time eval_ids reserved as held-out (never trained on) per agent.
HOLDOUT_EVAL_IDS: dict[str, tuple[str, ...]] = {
    "coordinator": (
        "hotel_search_miami",
        "expense_policy_over_limit",
        "expense_submit_within",
        "flight_search_no_results",
        "multi_intent_travel_expense",
    ),
    "travel": (
        "hotel_search_basic",
        "compare_flights",
        "ambiguous_destination",
    ),
    "expense": (
        "policy_check_over_limit",
        "check_before_submit",
        "unknown_category",
    ),
    "router": (
        "medium_high_expense_review_and_submit",
        "medium_high_book_and_policy_and_expense",
        "high_london_budget_trip",
        "high_multi_city_book_and_expense",
        "high_expense_audit_full",
    ),
}


def _eval_cases(agent: str) -> list[dict]:
    import json

    path = di.resolve(di.EVAL_EVALSETS[agent])
    data = json.loads(path.read_text())
    return data.get("eval_cases") or data.get("evalCases") or []


def holdout_prompts(agent: str) -> set[str]:
    """Normalized prompts reserved as held-out for ``agent`` (resolved via eval_id)."""
    ids = set(HOLDOUT_EVAL_IDS.get(agent, ()))
    prompts: set[str] = set()
    for case in _eval_cases(agent):
        if case.get("eval_id") in ids:
            text = di._first_user_text(case)
            if text.strip():
                prompts.add(di.normalize_prompt(text))
    return prompts
