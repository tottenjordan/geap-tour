"""Judge-vs-human calibration for the LLM autoraters (roadmap P1.6).

Every quality number in this repo is produced by an LLM judge. Nothing tracks
whether those judges actually agree with a *human* — so autorater drift or bias
(a judge that has quietly grown lenient, or over-rewards verbosity) is assumed
absent rather than measured (gap G1). This module makes it measurable: score a
fixed set of gold ``(prompt, response, human_score)`` cases with the judge and
report how well the judge tracks the human labels.

Because the gold cases carry a *frozen* response, calibration needs **no deployed
engine** — it exercises only the judge, so it is cheap and repeatable (unlike the
rubric evals, which need a live engine to generate responses). Run it over the
single default judge or the diverse :mod:`src.eval.judge_panel`.

Metrics (all on the shared 0-1 score axis):

* ``mae`` — mean absolute judge-vs-human error.
* ``bias`` — mean *signed* error (judge minus human); positive ⇒ judge is lenient.
* ``within_tolerance`` — fraction of cases where ``|judge - human| <= tolerance``
  (default ``0.2`` ≈ one rubric point on a 1-5 scale).
* ``pearson`` — linear correlation between judge and human scores.
* ``n_unparseable`` — cases where the judge produced no score (dropped from the
  stats, surfaced so silent judge failures are visible).

The scoring core is pure Python and unit-tested with fake judges (no GCP). A
``main()`` CLI prints the report and exits non-zero when calibration falls below
a threshold, so it doubles as a drift alarm.

**Honest caveat:** the shipped gold set (``data/policy_calibration_gold.json``)
is **author-curated single-annotator** labels, not independent multi-annotator
human annotation — a directional drift probe, not a validated gold standard.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# Default: within one rubric point (of 5) counts as agreement on the 0-1 axis.
DEFAULT_TOLERANCE = 0.2
# Default judge-vs-human agreement floor for the CLI PASS/FAIL (fraction within tol).
DEFAULT_MIN_WITHIN_TOLERANCE = 0.7

GOLD_SET_PATH = Path(__file__).parent / "data" / "policy_calibration_gold.json"
HUMAN_SCALE = 5.0  # gold human_score is a 1-5 rubric; normalized /5 to the 0-1 axis.


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Pearson correlation of two equal-length series; ``nan`` if undefined.

    Returns ``nan`` for fewer than two points or a zero-variance series (a
    correlation is not defined there).
    """
    n = len(xs)
    if n < 2 or n != len(ys):
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return float("nan")
    return sxy / math.sqrt(sxx * syy)


def calibration_metrics(
    human: Sequence[float],
    judge: Sequence[float | None],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict:
    """Judge-vs-human agreement over aligned score lists (both on the 0-1 axis).

    ``judge`` entries that are ``None`` (unparseable verdict) are dropped from the
    stats but counted in ``n_unparseable``. Returns ``mae``, ``bias``,
    ``within_tolerance``, ``pearson``, ``n`` (scored pairs), and ``n_unparseable``.
    """
    pairs = [(h, j) for h, j in zip(human, judge, strict=True) if j is not None]
    n_unparseable = len(judge) - len(pairs)
    if not pairs:
        return {
            "n": 0,
            "mae": float("nan"),
            "bias": float("nan"),
            "within_tolerance": float("nan"),
            "pearson": float("nan"),
            "n_unparseable": n_unparseable,
        }
    hs = [h for h, _ in pairs]
    js = [j for _, j in pairs]
    diffs = [j - h for h, j in pairs]
    within = sum(1 for d in diffs if abs(d) <= tolerance) / len(pairs)
    return {
        "n": len(pairs),
        "mae": sum(abs(d) for d in diffs) / len(pairs),
        "bias": sum(diffs) / len(pairs),
        "within_tolerance": within,
        "pearson": pearson(hs, js),
        "n_unparseable": n_unparseable,
    }


def load_gold_set(path: Path = GOLD_SET_PATH) -> list[dict]:
    """Load the curated gold cases (list of ``{prompt, response, human_score, metric}``)."""
    data = json.loads(path.read_text())
    return list(data["cases"])


def _human_axis(case: dict) -> float:
    """Normalize a gold case's 1-5 ``human_score`` to the 0-1 judge axis."""
    return float(case["human_score"]) / HUMAN_SCALE


def score_judge_vs_gold(
    gold: Sequence[dict],
    generate_fn: Callable[[str], str],
    build_prompt: Callable[[str, str], str],
    parse_fn: Callable[[str], float | None],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict:
    """Run a single judge over gold cases and report judge-vs-human agreement.

    Returns the :func:`calibration_metrics` keys plus ``per_case`` (each with the
    ``human`` and ``judge`` 0-1 scores) for inspection.
    """
    human: list[float] = []
    judge: list[float | None] = []
    per_case: list[dict] = []
    for case in gold:
        h = _human_axis(case)
        j = parse_fn(generate_fn(build_prompt(case["prompt"], case["response"])))
        human.append(h)
        judge.append(j)
        per_case.append({"prompt": case["prompt"], "human": h, "judge": j})
    metrics = calibration_metrics(human, judge, tolerance=tolerance)
    metrics["per_case"] = per_case
    return metrics


def score_panel_vs_gold(
    gold: Sequence[dict],
    judges: Sequence[Callable[[str], str]],
    build_prompt: Callable[[str, str], str],
    parse_fn: Callable[[str], float | None],
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict:
    """Run a judge *panel* over gold cases (median verdict) and report agreement.

    Adds a ``reliability`` block (panel inter-rater agreement over the gold set)
    to the :func:`score_judge_vs_gold` output — so a panel that disagrees with the
    human *and* internally is doubly flagged.
    """
    from src.eval.judge_panel import panel_reliability, score_with_panel

    human: list[float] = []
    judge: list[float | None] = []
    per_item_scores: list[list[float | None]] = []
    per_case: list[dict] = []
    for case in gold:
        h = _human_axis(case)
        result = score_with_panel(build_prompt(case["prompt"], case["response"]), judges, parse_fn)
        human.append(h)
        judge.append(result["median"])
        per_item_scores.append(result["per_judge"])
        per_case.append({"prompt": case["prompt"], "human": h, "judge": result["median"]})
    metrics = calibration_metrics(human, judge, tolerance=tolerance)
    metrics["per_case"] = per_case
    metrics["reliability"] = panel_reliability(per_item_scores)
    return metrics


def _default_judge(judge_model: str) -> Callable[[str], str]:
    from src.eval.judge_client import build_judge_generate_fn

    return build_judge_generate_fn(judge_model)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: score the policy judge against the gold set; non-zero if below floor."""
    import argparse

    from src.eval.policy_judge import DEFAULT_JUDGE_MODEL, build_policy_prompt, parse_policy_score

    parser = argparse.ArgumentParser(description="Judge-vs-human calibration (roadmap P1.6).")
    parser.add_argument("--panel", action="store_true", help="score with the diverse judge panel")
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    parser.add_argument("--min-within-tolerance", type=float, default=DEFAULT_MIN_WITHIN_TOLERANCE)
    args = parser.parse_args(argv)

    gold = load_gold_set()
    if args.panel:
        from src.eval.judge_panel import build_panel

        result = score_panel_vs_gold(
            gold, build_panel(), build_policy_prompt, parse_policy_score, tolerance=args.tolerance
        )
    else:
        result = score_judge_vs_gold(
            gold,
            _default_judge(args.judge_model),
            build_policy_prompt,
            parse_policy_score,
            tolerance=args.tolerance,
        )

    print(f"Calibration: {result['n']} gold cases ({result['n_unparseable']} unparseable)")
    print(f"  within ±{args.tolerance:.2f}: {result['within_tolerance']:.1%}")
    print(f"  MAE: {result['mae']:.3f}   bias (judge-human): {result['bias']:+.3f}")
    print(f"  Pearson r: {result['pearson']:.3f}")
    if "reliability" in result:
        rel = result["reliability"]
        print(f"  panel alpha: {rel['alpha']:.3f}   mean spread: {rel['mean_spread']:.3f}")

    ok = result["within_tolerance"] >= args.min_within_tolerance
    print(f"CALIBRATION: {'PASS' if ok else 'FAIL'} (floor {args.min_within_tolerance:.0%})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
