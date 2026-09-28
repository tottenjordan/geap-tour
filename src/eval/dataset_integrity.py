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
import re
from collections.abc import Iterable
from datetime import UTC, date, datetime
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


# A tool argument that is exactly a calendar date, e.g. ``"checkin_date": "2027-06-15"``.
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def past_tool_arg_dates(paths: Iterable[str | Path], today: date) -> list[str]:
    """Expected tool-argument dates that are already before ``today``.

    An evalset must hold concrete dates: ADK and GEPA read these files by path, so
    there is no load step to resolve a placeholder. But the search inventory and the
    agent both move with the calendar. Since #174, "June 15" means the next June 15.
    So a saved date goes stale once it passes, and the expected trajectory then
    names a date the agent will never search.

    No metric grades tool arguments today, so a stale date cannot fail anything.
    That is exactly why this check exists. It runs when an evalset is used and
    warns, never in CI, because a CI check tied to the calendar would turn red on a
    date instead of on a change. To fix: move the year forward, then
    ``dataset_manifest --update``.
    """
    stale = []
    for path in paths:
        data = json.loads(resolve(path).read_text())
        for case in data.get("eval_cases") or data.get("evalCases") or []:
            for turn in case.get("conversation") or []:
                uses = (turn.get("intermediate_data") or {}).get("tool_uses") or []
                for use in uses:
                    for arg, value in (use.get("args") or {}).items():
                        if (
                            isinstance(value, str)
                            and _ISO_DATE.match(value)
                            and date.fromisoformat(value) < today
                        ):
                            stale.append(
                                f"{path}: {case.get('eval_id', '?')} "
                                f"{use.get('name')}({arg}={value})"
                            )
    return stale


def warn_on_past_tool_arg_dates(paths: Iterable[str | Path], today: date | None = None) -> None:
    """Print one line per stale expected date (see :func:`past_tool_arg_dates`)."""
    stale = past_tool_arg_dates(paths, today or datetime.now(UTC).date())
    for line in stale:
        print(f"  Warning: expected tool-argument date has passed: {line}")
