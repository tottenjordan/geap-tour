"""Dataset integrity helpers — detect train/eval prompt contamination.

GEPA optimizes each agent on a ``train_eval_set`` (``src/agents/*/*.evalset.json``)
while the offline eval grades the agent on a separate eval-time evalset
(``src/eval/evalsets/*.evalset.json``). Historically these two families shared the
**same prompts**, so eval scores measured memorization, not generalization.

This module provides the pure primitives to measure that overlap and to enforce a
held-out split: prompts reserved for evaluation (see :mod:`src.eval.holdout`) must
never appear in the GEPA training set. The functions here have no GCP/SDK
dependency — they read the committed JSON evalsets — so they run in unit tests and
CI.
"""

from __future__ import annotations

import json
from pathlib import Path

# Repo root = three parents up from this file (src/eval/dataset_integrity.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]

# GEPA training evalsets (what the optimizer samples from) per agent key.
TRAIN_EVALSETS: dict[str, str] = {
    "coordinator": "src/agents/coordinator/coordinator_eval_set.evalset.json",
    "travel": "src/agents/travel_agent_opt/travel_eval_set.evalset.json",
    "expense": "src/agents/expense_agent_opt/expense_eval_set.evalset.json",
    "router": "src/router/router_eval_set.evalset.json",
}

# Eval-time evalsets (what the offline eval grades) per agent key.
EVAL_EVALSETS: dict[str, str] = {
    "coordinator": "src/eval/evalsets/coordinator.evalset.json",
    "travel": "src/eval/evalsets/travel_agent.evalset.json",
    "expense": "src/eval/evalsets/expense_agent.evalset.json",
    "router": "src/eval/evalsets/router_agent.evalset.json",
}


def normalize_prompt(text: str) -> str:
    """Canonicalize a prompt for comparison (whitespace + case insensitive)."""
    return " ".join(str(text).split()).strip().lower()


def _first_user_text(case: dict) -> str:
    """Extract the first user-turn text from an ADK evalset case."""
    for turn in case.get("conversation", []) or []:
        content = turn.get("user_content") or {}
        for part in content.get("parts", []) or []:
            text = part.get("text")
            if text:
                return str(text)
    return ""


def evalset_prompts(path: str | Path) -> list[str]:
    """Return the first-turn user prompts (raw, in file order) for an evalset JSON."""
    data = json.loads(Path(path).read_text())
    cases = data.get("eval_cases") or data.get("evalCases") or []
    return [_first_user_text(c) for c in cases]


def normalized_prompt_set(path: str | Path) -> set[str]:
    """Return the set of normalized first-turn prompts for an evalset JSON."""
    return {normalize_prompt(p) for p in evalset_prompts(path) if p.strip()}


def prompt_overlap(train_path: str | Path, eval_path: str | Path) -> set[str]:
    """Normalized prompts present in BOTH the train and eval evalsets (contamination)."""
    return normalized_prompt_set(train_path) & normalized_prompt_set(eval_path)


def resolve(path: str | Path) -> Path:
    """Resolve a repo-relative evalset path to an absolute Path."""
    p = Path(path)
    return p if p.is_absolute() else _REPO_ROOT / p
