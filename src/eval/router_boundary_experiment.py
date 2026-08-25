"""Does the router's cost-tuned tier boundary actually cost us any quality?

OUTCOME (2026-08-24): both bands settled, in opposite directions — ``COMPLEXITY_LOW``
moved 0.44 -> 0.25 (flash beats lite 18-1, replicated 14-2 on the served models) and
``COMPLEXITY_HIGH`` stayed at 0.80 (sonnet beats pro 12-2, replicated 17-1). The
metric this module was arguing with, ``routing_accuracy_pct``, has since been
re-scoped and renamed ``classifier_accuracy_pct``: it now grades the classifier on
fixed reference bands instead of the tunable cut-points, so it no longer moves when
routing is retuned. The motivating narrative below is kept as written, with the old
metric name, because it is what was true at the time.

`agent_router/routing_accuracy_pct` sat at **50%** against an 80% floor while
`cost_savings_pct` sits at **94.3%** against a 50% floor. Both are working as
specified, and they disagree, because they encode opposing goals: the DOE tuned
the cut-points for savings and got them.

The classifier is not broken. Its scores separate the bands with zero overlap —
low prompts score exactly ``0.10``, medium exactly ``0.40``, high ``0.75``-``0.90``
— and the boundaries simply slice between those levels by a hair:

* ``COMPLEXITY_LOW = 0.44`` sends every ``0.40`` "medium" prompt to **lite**
  (it would go to **flash** under a boundary below 0.40).
* ``COMPLEXITY_HIGH = 0.80`` sends every ``0.75`` "high" prompt to **sonnet**
  (it would go to **pro** under a boundary below 0.75).

That looks like a product trade-off, but it may not be one, and **that is what
this module measures**. ``routing_accuracy_pct`` scores agreement with a
complexity *label*; it says nothing about whether the answer was any good. If the
cheap tier handles the band as well as the expensive one, the savings are free and
the accuracy metric is measuring the wrong thing.

So: two paired side-by-side comparisons, cheap tier vs would-be tier, on every
prompt labelled for that band.

Why pairwise and not :mod:`src.eval.cross_model_experiment`: that module reads only
``summary_metrics`` (the ``/AVERAGE`` keys), so it yields means with **no per-case
scores** and cannot carry a confidence interval. A paired design is also more
sensitive for an A-vs-B question and costs two comparisons instead of fifteen runs.

PRE-REGISTERED DECISION RULE
----------------------------
Fixed here *before* the run, so a marginal number cannot be talked into whichever
answer is convenient. ``verdict()`` implements exactly this table and nothing else:

===================================  ==========================================
outcome                              action
===================================  ==========================================
``CANDIDATE_BETTER`` (p < 0.05,      The bigger model genuinely wins. Retune the
candidate wins)                      boundary below the band's score and accept
                                     the savings loss.
``BASELINE_BETTER`` (p < 0.05,       The *cheaper* model wins. The labels are
baseline wins)                       wrong; relabel and re-derive accuracy.
``NO_DIFFERENCE`` (not significant   No meaningful quality difference. The DOE was
AND the CI excludes a meaningful     right; ``routing_accuracy_pct`` is measuring
effect either way)                   label conformance, not outcome quality —
                                     re-scope or replace the metric.
``INCONCLUSIVE`` (not significant    Report the win-rate, its CI, and the decisive
and the CI still admits one)         n that would settle it. Change **nothing**.
===================================  ==========================================

``NO_DIFFERENCE`` is deliberately hard to reach: "we failed to find a difference"
is not "there is no difference" unless the interval can also rule one out. That is
the same three-valued shape as the calibration gate.

Read-only with respect to the repo and Cloud Monitoring: it drives engines and a
judge, and publishes no metrics.

Usage:
  uv run python -m src.eval.router_boundary_experiment --dry-run
  uv run python -m src.eval.router_boundary_experiment
  uv run python -m src.eval.router_boundary_experiment --band medium
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.eval.pairwise_eval import PairwiseConfig, run_pairwise_eval
from src.eval.stats import min_n_for_threshold

if TYPE_CHECKING:
    from collections.abc import Sequence

# A win-rate at or beyond this counts as "meaningfully better". Used only to decide
# whether a non-significant result is EQUIVALENCE (the interval rules out an effect
# this large in both directions) or merely UNDERPOWERED. 0.75 = the bigger model
# wins three cases for every one it loses; below that, tier choice is not what
# decides answer quality on these prompts.
EQUIVALENCE_BOUND = 0.75


@dataclass(frozen=True)
class Comparison:
    """One boundary miscut: the tier a band gets **now** vs the tier it would get.

    ``baseline`` is always the *current, cheaper* tier and ``candidate`` the
    *would-be, more expensive* one, because ``win_rate_candidate > 0.5`` reads as
    "the bigger model wins" — the direction the decision rule is written in.
    Swapping them would silently invert every verdict.
    """

    band: str
    baseline_agent: str
    candidate_agent: str
    boundary_var: str
    band_score: float
    note: str

    @property
    def boundary(self) -> str:
        """``NAME=value`` read from live config, never a hardcoded literal.

        The first version of this baked the value into the string. The moment the
        experiment succeeded and the cut-point moved, every report and log line
        still said ``COMPLEXITY_LOW=0.44`` — a stale number presented as the
        setting under test, which is the same class of defect the experiment was
        built to catch.
        """
        from src import config

        return f"{self.boundary_var}={getattr(config, self.boundary_var)}"

    @property
    def miscut_active(self) -> bool:
        """Is this band STILL being routed to the cheaper tier by the live cut?

        ``baseline_agent`` is documented as "the tier this band gets now", and that
        stops being true the moment the experiment succeeds and the cut-point moves
        below the band's score. Re-running afterwards is then a *historical*
        comparison — old tier vs current tier — and "retune the boundary" would be
        advice to redo something already done. The report says which one it is
        rather than letting the stale framing stand.
        """
        from src import config

        return getattr(config, self.boundary_var) > self.band_score


COMPARISONS: tuple[Comparison, ...] = (
    Comparison(
        band="medium",
        baseline_agent="lite_agent",
        candidate_agent="flash_agent",
        boundary_var="COMPLEXITY_LOW",
        band_score=0.40,
        note="medium prompts score 0.40; they run on lite whenever the cut sits above that",
    ),
    Comparison(
        band="high",
        baseline_agent="sonnet_agent",
        candidate_agent="pro_agent",
        boundary_var="COMPLEXITY_HIGH",
        band_score=0.75,
        note="high prompts score 0.75; they run on sonnet whenever the cut sits above that",
    ),
)


def select_cases(band: str) -> list[dict]:
    """Every prompt labelled ``band``, from both case sources, deduped on prompt.

    The disputed subset alone is too small to conclude on: at n=13 the sign test
    needs 11 wins and at n=7 it needs a perfect sweep. Using the whole band buys
    the power, and it is the honest scope anyway — the question is "does the cheap
    tier handle this band", not "does it handle these particular 13".

    The two sources have **different shapes** and that is easy to get wrong:
    :data:`ROUTER_EVAL_CASES` is a flat list whose items carry an
    ``expected_complexity`` field, while :data:`TIER_EVAL_CASES` is a
    ``{band: [cases]}`` dict whose items have no such field — the band is the key.
    Filtering both on ``expected_complexity`` would silently drop all 7 tier cases.
    """
    from src.eval.agent_eval_configs import ROUTER_EVAL_CASES
    from src.eval.tier_eval_cases import TIER_EVAL_CASES

    seen: set[str] = set()
    out: list[dict] = []
    for case in [c for c in ROUTER_EVAL_CASES if c.get("expected_complexity") == band] + list(
        TIER_EVAL_CASES.get(band, [])
    ):
        prompt = str(case["prompt"])
        if prompt in seen:
            continue
        seen.add(prompt)
        out.append(case)
    return out


def verdict(result: dict) -> dict:
    """Apply the pre-registered rule to one comparison's pairwise result.

    Reads ``result["significance"]`` (the sign test + Wilson CI that
    ``aggregate_choices`` already computed) — never recomputes it. Returns the
    verdict, a one-line reading, and for an inconclusive result the decisive ``n``
    that would settle it.
    """
    sig = result.get("significance") or {}
    decisive = int(sig.get("decisive", 0))
    rate = float(sig.get("win_rate_decisive", 0.0))
    ci_low, ci_high = float(sig.get("ci_low", 0.0)), float(sig.get("ci_high", 0.0))

    if sig.get("significant") and decisive:
        if rate > 0.5:
            return {
                "verdict": "CANDIDATE_BETTER",
                "reading": "the bigger model genuinely wins — retune the boundary",
                "needed_decisive_n": None,
            }
        return {
            "verdict": "BASELINE_BETTER",
            "reading": "the cheaper model wins — the complexity labels are wrong",
            "needed_decisive_n": None,
        }

    # Not significant. Equivalence requires the interval to rule out a meaningful
    # effect in BOTH directions; otherwise this is underpowered, not a null result.
    if decisive and ci_high < EQUIVALENCE_BOUND and ci_low > 1 - EQUIVALENCE_BOUND:
        return {
            "verdict": "NO_DIFFERENCE",
            "reading": (
                "no meaningful quality difference — the cut-point is not what "
                "decides answer quality on these prompts"
            ),
            "needed_decisive_n": None,
        }
    return {
        "verdict": "INCONCLUSIVE",
        "reading": "cannot tell at this sample size — change nothing",
        "needed_decisive_n": min_n_for_threshold(rate or 0.5, EQUIVALENCE_BOUND),
    }


def annotate_per_case(per_case: list[dict], *, classify=None) -> list[dict]:
    """Add each prompt's classifier ``score`` and routed ``tier`` in place.

    Without this a completed run is not re-analysable: ``per_case`` held only
    ``{prompt, choice}``, so asking "did the winner hold on the *sub-band* the
    router splits at ``COMPLEXITY_HIGH``?" needed a whole second paid run. A band
    is not homogeneous — the high band spans 0.75/0.85/0.90 and the router sends
    those to two different tiers — so the pooled win-rate can hide a split.

    Best-effort by design: the classifier is a network call, and losing the
    annotation must never cost a run whose expensive part already succeeded. On
    failure the entry simply has no ``score``/``tier``.
    """
    import asyncio

    from src.router.complexity import classify_complexity, score_to_model_tier

    classify = classify or classify_complexity

    async def _run() -> None:
        for entry in per_case:
            try:
                res = await classify(entry["prompt"])
                entry["score"] = res.score
                entry["tier"] = score_to_model_tier(res.score)
            except Exception as exc:  # never lose a completed run to annotation
                entry["annotation_error"] = str(exc)[:120]

    import contextlib

    with contextlib.suppress(Exception):
        asyncio.run(_run())
    return per_case


def subband_split(result: dict, boundary: float) -> dict:
    """Split one comparison's decisive cases at ``boundary`` and score each side.

    The high band is the motivating case: the pooled run said sonnet beats pro
    17-1, but the router sends only the sub-``COMPLEXITY_HIGH`` prompts to sonnet
    and the rest to pro. If the win does not hold on the upper sub-band, those are
    misrouted.

    Each side gets its own :func:`win_rate_significance`, because a pooled result
    is not evidence about a subset. Entries without a ``score`` are excluded and
    counted in ``unscored`` — silently dropping them would shrink a denominator.
    """
    from src.eval.pairwise_eval import BASELINE, CANDIDATE
    from src.eval.stats import win_rate_significance

    sides: dict[str, dict[str, int]] = {
        "below": {"wins": 0, "losses": 0},
        "at_or_above": {"wins": 0, "losses": 0},
    }
    unscored = 0
    for entry in result.get("per_case") or []:
        score = entry.get("score")
        if score is None:
            unscored += 1
            continue
        side = sides["below" if score < boundary else "at_or_above"]
        if entry.get("choice") == CANDIDATE:
            side["wins"] += 1
        elif entry.get("choice") == BASELINE:
            side["losses"] += 1

    return {
        "boundary": boundary,
        "unscored": unscored,
        **{
            name: {
                **counts,
                "significance": win_rate_significance(counts["wins"], counts["losses"]),
            }
            for name, counts in sides.items()
        },
    }


def run_comparison(
    comparison: Comparison,
    *,
    config: PairwiseConfig | None = None,
    run_fn=None,
    cases: Sequence[dict] | None = None,
    engines: dict[str, str] | None = None,
) -> dict:
    """Run one band's paired SxS and attach its verdict + drop accounting.

    ``dropped`` is load-bearing, not decoration: ``run_pairwise_eval`` silently
    skips any case where either engine returned an empty-or-error response, and
    this repo has four documented causes of exactly that. An unreported drop makes
    a halved denominator look like a clean one.
    """
    from src.eval.batch_eval import _resolve_agent_resource_name
    from src.eval.cross_model_experiment import EXPERIMENT_AGENTS

    engines = engines or EXPERIMENT_AGENTS
    run_fn = run_fn or run_pairwise_eval
    band_cases = list(cases if cases is not None else select_cases(comparison.band))

    result = run_fn(
        _resolve_agent_resource_name(engines[comparison.baseline_agent]),
        _resolve_agent_resource_name(engines[comparison.candidate_agent]),
        cases=band_cases,
        config=config or PairwiseConfig(sampling_count=4, flip_enabled=True),
    )
    judged = int(result.get("n_cases", 0))
    annotate_per_case(result.get("per_case") or [])
    decided = verdict(result)
    if decided["verdict"] == "CANDIDATE_BETTER" and not comparison.miscut_active:
        # The cut already moved; the bigger model winning now CONFIRMS the shipped
        # boundary instead of arguing for a change.
        decided = {**decided, "reading": "confirms the current boundary — no change needed"}
    from src import config

    return {
        **result,
        "band": comparison.band,
        "boundary": comparison.boundary,
        "subband": subband_split(result, getattr(config, comparison.boundary_var)),
        "miscut_active": comparison.miscut_active,
        "baseline_agent": comparison.baseline_agent,
        "candidate_agent": comparison.candidate_agent,
        "n_selected": len(band_cases),
        "n_judged": judged,
        "dropped": len(band_cases) - judged,
        **decided,
    }


def format_report(results: Sequence[dict]) -> str:
    """Human-readable summary — one block per comparison, verdict last."""
    lines = ["", "=" * 74, "ROUTER BOUNDARY EXPERIMENT", "=" * 74]
    for r in results:
        sig = r.get("significance") or {}
        live = r.get("miscut_active", True)
        roles = ("current", "would-be") if live else ("old", "current")
        lines += [
            "",
            f"[{r['band']}] {r['baseline_agent']} ({roles[0]}) vs "
            f"{r['candidate_agent']} ({roles[1]}) — {r['boundary']}"
            + ("" if live else "  [miscut already fixed — historical comparison]"),
            f"  cases:     {r['n_selected']} selected, {r['n_judged']} judged, "
            f"{r['dropped']} dropped (empty/error)",
            f"  decisive:  {sig.get('decisive', 0)} "
            f"({sig.get('wins', 0)} candidate / {sig.get('losses', 0)} baseline, "
            f"tie rate {r.get('tie_rate', 0.0):.0%})",
            f"  win rate:  {sig.get('win_rate_decisive', 0.0):.1%} for the bigger model "
            f"[95% CI {sig.get('ci_low', 0.0):.1%}-{sig.get('ci_high', 0.0):.1%}]",
            f"  p-value:   {sig.get('p_value', 1.0):.4f}",
        ]
        # A band is not homogeneous: the router splits it at this very cut-point and
        # sends the two halves to different tiers, so a pooled win can hide a side
        # that goes the other way. Print both, with their own significance.
        if sub := r.get("subband"):
            for side, label in (("below", "below cut"), ("at_or_above", "at/above cut")):
                s = sub[side]["significance"]
                if not s["decisive"]:
                    continue
                verdict_word = "significant" if s["significant"] else "UNDERPOWERED"
                lines.append(
                    f"  {label:<13} {s['wins']}-{s['losses']} "
                    f"({s['win_rate_decisive']:.0%} for the bigger model, "
                    f"p={s['p_value']:.4f}, {verdict_word})"
                )
            if sub["unscored"]:
                lines.append(f"  (unscored:  {sub['unscored']} cases had no classifier score)")
        lines += [f"  VERDICT:   {r['verdict']} — {r['reading']}"]
        if r.get("needed_decisive_n"):
            lines.append(f"             needs ~{r['needed_decisive_n']} decisive cases to settle")
    lines += ["", "=" * 74, ""]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI. Dry-run prints the plan and case counts and spends nothing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--band", choices=[c.band for c in COMPARISONS], help="run one band only")
    parser.add_argument("--judge-model", default=PairwiseConfig().judge_model)
    parser.add_argument("--sampling-count", type=int, default=4)
    parser.add_argument("--no-flip", action="store_true", help="disable flip debiasing")
    parser.add_argument("--json", action="store_true", help="emit raw JSON instead of the report")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; spend nothing")
    args = parser.parse_args(argv)

    from src.eval.cross_model_experiment import EXPERIMENT_AGENTS, preflight_engines

    comparisons = [c for c in COMPARISONS if args.band in (None, c.band)]
    agents = sorted({a for c in comparisons for a in (c.baseline_agent, c.candidate_agent)})

    if args.dry_run:
        print("[dry-run] router boundary experiment plan:")
        for c in comparisons:
            n = len(select_cases(c.band))
            state = "live miscut" if c.miscut_active else "ALREADY FIXED — historical"
            print(
                f"  [{c.band}] {c.baseline_agent} -> {c.candidate_agent}  "
                f"({n} cases; {c.boundary}; {state}; {c.note})"
            )
        print(f"  judge={args.judge_model} sampling={args.sampling_count} flip={not args.no_flip}")
        wired = ", ".join(f"{a}={EXPERIMENT_AGENTS.get(a) or 'UNSET'}" for a in agents)
        print(f"  engines: {wired}")
        return 0

    if dead := preflight_engines(agents):
        print("Cannot run — engines do not resolve:")
        for line in dead:
            print(f"  {line}")
        return 1

    config = PairwiseConfig(
        sampling_count=args.sampling_count,
        flip_enabled=not args.no_flip,
        judge_model=args.judge_model,
    )
    results = [run_comparison(c, config=config) for c in comparisons]
    print(json.dumps(results, indent=2, sort_keys=True) if args.json else format_report(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
