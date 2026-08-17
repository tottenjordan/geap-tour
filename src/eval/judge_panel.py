"""Multi-model LLM-judge panel + inter-rater agreement (roadmap P1.4).

Every LLM-judge scorer in this repo (``policy_judge``, ``tool_use_judge``,
``online_monitor``, ``pairwise_eval``) historically trusted a **single** judge
model. A single autorater is a single point of bias: if that one model has a
systematic blind spot (over-rewards verbosity, mis-reads a domain), its verdict
is the eval, unchecked. Raising the judge temperature to sample it repeatedly
does not help — the judges are pinned to ``temperature=0`` for reproducibility
(see :mod:`src.eval.judge_client`), so self-consistency sampling is degenerate.

This module scores each item with a **panel of diverse models** (spanning
Gemini-2, Gemini-3 and Claude families, so no single family's blind spot decides
the outcome), aggregates per item with the **median** (robust to one contrarian
judge), and reports **inter-rater reliability** so a low-agreement panel is
visible rather than silently averaged away:

* :func:`median_score` / :func:`score_spread` — robust per-item aggregate + a
  cheap per-item disagreement signal (score range).
* :func:`majority_label` — mode + agreement fraction for categorical verdicts.
* :func:`krippendorff_alpha_interval` — Krippendorff's alpha for interval data,
  the standard IRR for numeric ratings with any number of raters and missing
  values (``None`` verdicts an individual judge failed to produce).
* :func:`score_with_panel` / :func:`score_pairs_with_panel` — run a panel over a
  prompt / a batch of (prompt, response) pairs.
* :func:`build_panel` — build one deterministic+retrying judge fn per model
  (delegates to :func:`src.eval.judge_client.build_judge_generate_fn`); the
  factory is injectable so the panel logic is unit-tested with no GCP calls.

The aggregation core is pure Python (``math``/``statistics``) — no cloud.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# Three diverse judge backbones: a Gemini-2 tier, a Gemini-3 tier, and a Claude
# tier. Cross-family so a single model family's systematic bias cannot decide the
# verdict. All run at temperature=0 via judge_client (deterministic).
DEFAULT_PANEL_MODELS: tuple[str, ...] = (
    "gemini-2.5-flash",
    "gemini-3.5-flash",
    "claude-sonnet-4-6",
)


def median_score(scores: Sequence[float | None]) -> float | None:
    """Median of the valid (non-``None``) scores, or ``None`` if none are valid.

    The median is deliberately robust: one contrarian judge in a panel of three
    cannot swing the aggregate the way a mean would.
    """
    valid = [float(s) for s in scores if s is not None]
    if not valid:
        return None
    return float(statistics.median(valid))


def score_spread(scores: Sequence[float | None]) -> float:
    """Max-minus-min of the valid scores — a cheap per-item disagreement signal.

    Returns ``0.0`` when fewer than two judges produced a score (nothing to
    disagree about).
    """
    valid = [float(s) for s in scores if s is not None]
    if len(valid) < 2:
        return 0.0
    return max(valid) - min(valid)


def majority_label(labels: Sequence[str | None]) -> tuple[str | None, float]:
    """Most-common non-``None`` label and the fraction of valid judges backing it.

    Returns ``(None, 0.0)`` when no judge produced a label. On a tie the
    lexicographically-first winner is chosen (deterministic).
    """
    valid = [str(x) for x in labels if x is not None]
    if not valid:
        return (None, 0.0)
    counts = Counter(valid)
    top = max(counts.items(), key=lambda kv: (kv[1], _neg_key(kv[0])))
    return (top[0], top[1] / len(valid))


def _neg_key(label: str) -> tuple[int, ...]:
    """Tie-break key so ``max`` prefers the lexicographically-first label."""
    return tuple(-ord(c) for c in label)


def krippendorff_alpha_interval(items: Sequence[Sequence[float | None]]) -> float:
    """Krippendorff's alpha (interval metric) across items rated by the panel.

    ``items`` is one row per unit, each row the per-judge scores (``None`` for a
    judge that produced no verdict). Only units with ≥2 valid ratings contribute
    ("pairable values"). Returns ``nan`` when fewer than two pairable values
    exist, and ``1.0`` when there is no variance at all (perfect agreement).

    alpha = 1 - Do/De, where Do is the observed within-unit disagreement and De
    the disagreement expected if ratings were assigned at random. alpha=1 is
    perfect agreement, 0 is chance, <0 is systematic disagreement.
    """
    units = [[float(v) for v in row if v is not None] for row in items]
    units = [u for u in units if len(u) >= 2]
    n = sum(len(u) for u in units)
    if n < 2:
        return float("nan")

    # Observed disagreement: within-unit squared differences, weighted 1/(m-1).
    # For interval data, sum_{i<j}(xi-xj)^2 == m*sum(x^2) - (sum x)^2.
    observed = 0.0
    for u in units:
        m = len(u)
        s1 = math.fsum(u)
        s2 = math.fsum(x * x for x in u)
        within = m * s2 - s1 * s1
        observed += (2.0 / (m - 1)) * within
    d_o = observed / n

    # Expected disagreement: over all pairable values pooled together.
    allv = [x for u in units for x in u]
    big1 = math.fsum(allv)
    big2 = math.fsum(x * x for x in allv)
    d_e = (2.0 * (n * big2 - big1 * big1)) / (n * (n - 1))
    if d_e == 0:
        return 1.0
    return 1.0 - d_o / d_e


def score_with_panel(
    prompt: str,
    judges: Sequence[Callable[[str], str]],
    parse_fn: Callable[[str], float | None],
) -> dict:
    """Score one already-rendered ``prompt`` with every judge in the panel.

    Returns ``per_judge`` (the parsed score per judge, ``None`` where a judge's
    verdict was unparseable), the robust ``median``, the ``spread``
    (disagreement), and ``n_valid`` (judges that produced a score).
    """
    per_judge = [parse_fn(judge(prompt)) for judge in judges]
    return {
        "per_judge": per_judge,
        "median": median_score(per_judge),
        "spread": score_spread(per_judge),
        "n_valid": sum(1 for s in per_judge if s is not None),
    }


def panel_reliability(per_item_scores: Sequence[Sequence[float | None]]) -> dict:
    """Panel-level inter-rater reliability over a batch of per-item judge scores.

    ``per_item_scores`` is one row per item, each row the panel's per-judge
    scores. Returns Krippendorff's ``alpha``, the ``mean_spread`` (mean per-item
    score range over items with ≥2 valid judges), and the item/judge counts.
    """
    spreads = [
        score_spread(row) for row in per_item_scores if sum(1 for v in row if v is not None) >= 2
    ]
    return {
        "alpha": krippendorff_alpha_interval(per_item_scores),
        "mean_spread": (sum(spreads) / len(spreads)) if spreads else 0.0,
        "n_items": len(per_item_scores),
        "n_judges": max((len(row) for row in per_item_scores), default=0),
    }


def score_pairs_with_panel(
    pairs: Sequence[tuple[str, str]],
    judges: Sequence[Callable[[str], str]],
    build_prompt: Callable[[str, str], str],
    parse_fn: Callable[[str], float | None],
) -> dict:
    """Score every ``(prompt, response)`` pair with the panel; mean of medians.

    ``build_prompt`` renders the judge rubric for a pair; ``parse_fn`` extracts a
    0-1 score from a judge's raw text. Returns the mean of the per-item panel
    medians (unparseable items dropped, not zeroed), the scored/total counts, and
    a ``reliability`` block (:func:`panel_reliability`).
    """
    medians: list[float] = []
    per_item_scores: list[list[float | None]] = []
    for prompt, response in pairs:
        result = score_with_panel(build_prompt(prompt, response), judges, parse_fn)
        per_item_scores.append(result["per_judge"])
        if result["median"] is not None:
            medians.append(result["median"])
    return {
        "score": (sum(medians) / len(medians)) if medians else None,
        "n_scored": len(medians),
        "n_total": len(pairs),
        "reliability": panel_reliability(per_item_scores),
    }


def build_panel(
    models: Sequence[str] = DEFAULT_PANEL_MODELS,
    *,
    generate_fn_factory: Callable[[str], Callable[[str], str]] | None = None,
    project: str | None = None,
    location: str | None = None,
) -> list[Callable[[str], str]]:
    """Build one ``prompt -> judge_text`` fn per model in the panel.

    Each judge is deterministic (temperature=0) and retrying — the shared
    :func:`src.eval.judge_client.build_judge_generate_fn`. ``generate_fn_factory``
    is injectable so the panel logic can be unit-tested with fakes (no GCP).
    """
    if generate_fn_factory is None:
        from src.eval.judge_client import build_judge_generate_fn

        def generate_fn_factory(model: str) -> Callable[[str], str]:
            return build_judge_generate_fn(model, project, location)

    return [generate_fn_factory(model) for model in models]
