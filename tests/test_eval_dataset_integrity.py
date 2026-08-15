"""Enforce the train/eval held-out split (no GEPA/eval prompt contamination)."""

from __future__ import annotations

import pytest

from src.eval import dataset_integrity as di
from src.eval.holdout import HOLDOUT_EVAL_IDS, holdout_prompts

AGENTS = ("coordinator", "travel", "expense", "router")


@pytest.mark.parametrize("agent", AGENTS)
def test_evalsets_parse_and_nonempty(agent: str) -> None:
    train = di.normalized_prompt_set(di.resolve(di.TRAIN_EVALSETS[agent]))
    ev = di.normalized_prompt_set(di.resolve(di.EVAL_EVALSETS[agent]))
    assert train, f"{agent} train evalset has no prompts"
    assert ev, f"{agent} eval evalset has no prompts"


@pytest.mark.parametrize("agent", AGENTS)
def test_holdout_ids_resolve_to_eval_prompts(agent: str) -> None:
    """Every declared holdout eval_id must exist in the eval-time evalset."""
    resolved = holdout_prompts(agent)
    declared = len(HOLDOUT_EVAL_IDS[agent])
    assert declared > 0, f"{agent} declares no holdout"
    assert len(resolved) == declared, (
        f"{agent}: {declared} holdout ids declared but {len(resolved)} resolved — "
        "an id is missing/renamed in the eval-time evalset"
    )


@pytest.mark.parametrize("agent", AGENTS)
def test_holdout_is_graded(agent: str) -> None:
    """Held-out prompts must be present in the eval-time (graded) evalset."""
    ev = di.normalized_prompt_set(di.resolve(di.EVAL_EVALSETS[agent]))
    assert holdout_prompts(agent) <= ev


@pytest.mark.parametrize("agent", AGENTS)
def test_holdout_disjoint_from_training(agent: str) -> None:
    """The core guard: held-out prompts must NEVER appear in the GEPA train set."""
    train = di.normalized_prompt_set(di.resolve(di.TRAIN_EVALSETS[agent]))
    leaked = holdout_prompts(agent) & train
    assert not leaked, (
        f"{agent}: {len(leaked)} held-out prompt(s) leaked into the GEPA training "
        f"evalset {di.TRAIN_EVALSETS[agent]} — remove them from training:\n"
        + "\n".join(f"  - {p[:70]}" for p in sorted(leaked))
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
