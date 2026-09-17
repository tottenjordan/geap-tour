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
        # Renamed 2026-08-21: the case was expense-only (its prompt asks only to
        # submit a receipt) but still carried a leftover search_flights expectation
        # from when it was genuinely multi-intent. See
        # docs/notes/gepa-sampler-case-audit.md.
        "expense_submit_low_amount",
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


# ---------------------------------------------------------------------------
# The scored collection — a THIRD dataset, and the one that is published.
#
# Everything above guards `src/eval/evalsets/*`. But `multi_agent_batch_eval`
# scores `src/eval/agent_eval_configs.py` and publishes THAT to `agent_eval/*`,
# and nothing connected the two: the holdout machinery has been CI-enforced since
# PR #116 while pointing at a collection the published number never touches.
#
# Measured 2026-09-17: 9 of 16 held-out probes do not appear in the scored set at
# all (travel 0/3, router 0/5, expense 1/3, coordinator 3/5). Closing that is 2b —
# it needs new domain-authored cases. What these helpers add is the measurement,
# so the gap is visible and bounded instead of merely true.
# ---------------------------------------------------------------------------

#: `agent_eval_configs` agent name -> the key used by HOLDOUT_EVAL_IDS / dataset_integrity.
SCORED_AGENT_KEYS: dict[str, str] = {
    "coordinator_agent": "coordinator",
    "travel_agent": "travel",
    "expense_agent": "expense",
    "router_agent": "router",
}


def scored_prompts(agent_name: str) -> list[str]:
    """Normalized prompts from the SCORED collection, in their scored order.

    A list, not a set: :func:`multi_agent_batch_eval._select_cases` needs to reorder
    the real cases, and set iteration order would make the CI gate's sample
    non-deterministic between runs.
    """
    # Imported here, not at module scope: this module is deliberately GCP-free so it
    # runs in unit tests, and agent_eval_configs pulls in the evaluation SDK types.
    from src.eval.agent_eval_configs import get_eval_cases

    return [di.normalize_prompt(c["prompt"]) for c in get_eval_cases(agent_name)]


def scored_contamination(agent_name: str) -> dict[str, int]:
    """How trustworthy is the published score for ``agent_name``?

    ``contaminated`` counts scored prompts that GEPA also trained on — those grade
    memorization, not generalization. ``holdout_present`` counts how many of the
    reserved generalization probes actually reach the scored set.

    Returns zeros for an agent with no holdout mapping rather than raising: this is
    diagnostic output printed beside a score, and it must never be the reason an
    eval run dies.
    """
    key = SCORED_AGENT_KEYS.get(agent_name)
    if key is None:
        return {"scored": 0, "contaminated": 0, "holdout_present": 0, "holdout_total": 0}

    scored = scored_prompts(agent_name)
    train = di.normalized_prompt_set(di.TRAIN_EVALSETS[key])
    held = holdout_prompts(key)
    return {
        "scored": len(scored),
        "contaminated": sum(1 for p in scored if p in train),
        "holdout_present": sum(1 for p in set(scored) if p in held),
        "holdout_total": len(held),
    }


def is_holdout_case(agent_name: str, case: dict) -> bool:
    """Is this scored case one of the reserved (never-trained-on) probes?"""
    key = SCORED_AGENT_KEYS.get(agent_name)
    if key is None:
        return False
    return di.normalize_prompt(case.get("prompt", "")) in holdout_prompts(key)
