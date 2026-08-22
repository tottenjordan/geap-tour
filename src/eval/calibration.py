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

**Judge agreement is only interpretable against a HUMAN ceiling.** "96% within
tolerance" says nothing on its own: if two careful annotators only agree with
*each other* 85% of the time, a judge at 84% is performing at human level, and a
judge at 96% is suspiciously well-fitted to one annotator's idiosyncrasies. So
the gold cases carry per-annotator scores in ``annotations`` (``human_score`` is
the derived median), and :func:`annotator_reliability` reports Krippendorff's
alpha among the humans — reusing the exact function the judge panel uses, so the
two alphas are directly comparable. Collect a second pass with
``python -m src.eval.annotate``.

**Honest caveat:** the gold set (``data/policy_calibration_gold.json``) is
author-curated, and its two annotation passes are the same operator's — so a2 is
independent of a1's *labels* but not of a1's *framing*. A probe with a known
ceiling, not a validated benchmark.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# Default: within one rubric point (of 5) counts as agreement on the 0-1 axis.
# Provisional: a round number, not a derived one. The distribution of human-human
# deltas (once a second annotator exists) is what should set it.
DEFAULT_TOLERANCE = 0.2

# Judge-vs-human agreement floor for the CLI PASS/FAIL (fraction within tolerance).
#
# **Provisional, and it must be re-derived.** 0.7 was chosen against a gold set
# that was effectively binary — stark good/bad contrasts a judge separates
# trivially, which is why agreement sat at 100% and the gate could only ever move
# down. The 20 ambiguous `difficulty: "hard"` cases added in gold v3 will pull the
# headline number down *by design* once they are annotated. At that point set this
# from the measured human ceiling (see `annotator_reliability`) rather than from a
# round number: a floor above what two humans manage on the same cases is
# unachievable, and one far below it never fires. Until those cases are scored the
# gate runs on the 32 contrast cases only, where 0.7 remains valid.
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


def annotator_scores(case: dict) -> list[float]:
    """The per-annotator 1-5 scores for a case, in a stable annotator order.

    Falls back to a single-element list from the legacy ``human_score`` so a case
    written before the multi-annotator schema still works.
    """
    annotations = case.get("annotations")
    if annotations:
        return [float(annotations[k]) for k in sorted(annotations)]
    return [float(case["human_score"])] if case.get("human_score") is not None else []


def consensus_score(case: dict) -> float | None:
    """The case's agreed 1-5 score: the median across annotators.

    Median, not mean, so one outlying annotator cannot drag the reference — the
    same reason :mod:`src.eval.judge_panel` aggregates its panel by median.
    Returns ``None`` for an unscored case (a hard case awaiting annotation), which
    callers must filter rather than treat as zero.
    """
    from src.eval.judge_panel import median_score

    scores = annotator_scores(case)
    return median_score(scores) if scores else None


def scored_cases(cases: Sequence[dict]) -> list[dict]:
    """Only the cases that have at least one annotation — the rest aren't gradable."""
    return [c for c in cases if consensus_score(c) is not None]


def annotator_reliability(cases: Sequence[dict]) -> dict:
    """Human-vs-human agreement: Krippendorff's alpha over the annotator columns.

    This is the **ceiling** for judge agreement. Reuses
    :func:`src.eval.judge_panel.krippendorff_alpha_interval` unchanged — it takes
    one row of ratings per unit and does not care whether the raters are models or
    people, which makes the human alpha and the judge-panel alpha comparable
    numbers rather than two similar-sounding ones.

    Only cases rated by ≥2 annotators are pairable, so ``alpha`` is ``nan`` until
    a second pass exists. Scores are compared on the raw 1-5 scale.
    """
    from src.eval.judge_panel import krippendorff_alpha_interval, score_spread

    rows = [annotator_scores(c) for c in cases]
    pairable = [r for r in rows if len(r) >= 2]
    names: set[str] = set()
    for case in cases:
        names.update(case.get("annotations") or {})
    spreads = [score_spread(r) for r in pairable]
    return {
        "alpha": krippendorff_alpha_interval(rows),
        "mean_spread": (sum(spreads) / len(spreads)) if spreads else float("nan"),
        "n_annotators": len(names),
        "n_pairable": len(pairable),
        "annotators": sorted(names),
    }


def annotator_disagreements(cases: Sequence[dict], min_delta: float = 1.0) -> list[dict]:
    """Cases where annotators differ by ``min_delta`` rubric points or more.

    These are where "correct" is genuinely contested. They deserve re-wording or
    exclusion rather than silently penalising the judge for picking a side.
    """
    from src.eval.judge_panel import score_spread

    out = []
    for case in cases:
        scores = annotator_scores(case)
        if len(scores) >= 2 and score_spread(scores) >= min_delta:
            out.append(
                {
                    "prompt": case["prompt"],
                    "response": case["response"],
                    "scores": dict(case.get("annotations") or {}),
                    "spread": score_spread(scores),
                }
            )
    return sorted(out, key=lambda c: -c["spread"])


def _human_axis(case: dict) -> float:
    """Normalize a gold case's consensus 1-5 score to the 0-1 judge axis."""
    consensus = consensus_score(case)
    if consensus is None:
        raise ValueError(f"case has no annotations: {case.get('prompt', '?')!r}")
    return consensus / HUMAN_SCALE


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


def ceiling_verdict(judge_agreement: float, human_alpha: float, margin: float = 0.05) -> str:
    """Phrase judge agreement RELATIVE to what humans manage on the same cases.

    The claim worth making is not "the judge scores 0.91" but "the judge is at the
    human ceiling". Below the ceiling by more than ``margin`` is a judge problem;
    conspicuously *above* it is a warning, not a triumph — it usually means the
    judge has fitted one annotator's idiosyncrasies rather than the rubric.
    """
    if math.isnan(judge_agreement) or math.isnan(human_alpha):
        return "human ceiling unavailable (need >= 2 annotators on shared cases)"
    delta = judge_agreement - human_alpha
    if delta < -margin:
        return (
            f"judge agreement ({judge_agreement:.3f}) is BELOW the human ceiling "
            f"({human_alpha:.3f}) — the judge, not the cases, is the weak link"
        )
    if delta > margin:
        return (
            f"judge agreement ({judge_agreement:.3f}) EXCEEDS human agreement "
            f"({human_alpha:.3f}) — suspicious: likely fitted to one annotator "
            "rather than to the rubric"
        )
    return (
        f"judge agreement ({judge_agreement:.3f}) is AT the human ceiling "
        f"({human_alpha:.3f}) — as good as the labels allow"
    )


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
    parser.add_argument(
        "--annotators",
        action="store_true",
        help="also report human-vs-human agreement (the ceiling the judge is measured against)",
    )
    args = parser.parse_args(argv)

    all_cases = load_gold_set()
    gold = scored_cases(all_cases)
    skipped = len(all_cases) - len(gold)
    if skipped:
        # Loud, not silent: an unannotated case is missing evidence, and a
        # calibration number quietly computed over a subset is how a gate lies.
        print(f"NOTE: {skipped} case(s) have no annotations yet and are excluded.")
        print("      Run `python -m src.eval.annotate --annotator <id>` to score them.")

    if args.annotators:
        rel = annotator_reliability(gold)
        disagreements = annotator_disagreements(gold)
        if rel["n_annotators"] < 2:
            print(
                f"Annotators: {rel['n_annotators']} ({', '.join(rel['annotators']) or 'none'}) "
                "— no human ceiling yet; judge agreement is uninterpretable on its own."
            )
        else:
            print(
                f"Annotators: alpha {rel['alpha']:.3f} "
                f"({rel['n_annotators']} raters, {rel['n_pairable']} doubly-rated cases, "
                f"{len(disagreements)} disagreeing by >= 1 point)"
            )
            for case in disagreements[:5]:
                scores = ", ".join(f"{k}={v}" for k, v in sorted(case["scores"].items()))
                print(f"    contested ({scores}): {case['prompt'][:58]}")

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

    if args.annotators:
        human = annotator_reliability(gold)
        if human["n_annotators"] >= 2 and not math.isnan(human["alpha"]):
            print(f"  -> {ceiling_verdict(result['pearson'], human['alpha'])}")

    ok = result["within_tolerance"] >= args.min_within_tolerance
    print(f"CALIBRATION: {'PASS' if ok else 'FAIL'} (floor {args.min_within_tolerance:.0%})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
