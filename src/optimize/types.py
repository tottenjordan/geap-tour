"""Shape of a GEPA optimization summary."""

from __future__ import annotations

from typing import TypedDict


class GepaSummary(TypedDict):
    """What an optimization run produced, and what it cost to find out.

    ``lift`` is the whole verdict — ``best_score`` alone says nothing without the
    ``baseline_score`` it beat. ``total_metric_calls`` and ``num_full_val_evals``
    are the price: an optimizer that found a 2% lift over 400 evaluations has not
    obviously earned its keep, and that judgement needs both numbers present.

    ``optimized_instruction`` is the only field that changes the agent. Everything
    else is evidence for whether to apply it.

    **The scores are Optional and that is the honest shape.** A run that produced no
    best candidate reports ``None``, not ``0.0`` — the producer builds ``lift`` only
    when both scores exist, precisely so a failed optimization cannot render as a
    zero-lift successful one. Declaring these ``float`` would have forced a default
    somewhere and erased the difference.
    """

    baseline_score: float | None
    best_score: float | None
    lift: float | None
    best_idx: int
    num_candidates: int
    num_full_val_evals: int | None
    total_metric_calls: int | None
    val_aggregate_scores: list[float]
    optimized_instruction: str | None
