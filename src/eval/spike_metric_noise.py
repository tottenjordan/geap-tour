"""How much does a rubric score move when NOTHING changed?

A monitored metric can only detect a regression larger than its own noise. Every
threshold in ``quality_alerts`` is a claim about that, and until 2026-09-17 none of
them had a measured noise figure behind it — the floors were set from observed
*levels*, which says where a metric sits but not how much it wanders.

This spike answers it by separating the two sources, which need different fixes:

* **judge variance** — score ONE frozen inference capture N times. Everything that
  moves is the autorater disagreeing with itself. Fixed by a judge panel
  (:mod:`src.eval.judge_panel`), more samples, or a better rubric.
* **agent variance** — the rest of the spread across full runs over identical
  cases against the same engine. Fixed, if at all, by the agent.

Variances add, so ``agent_sd = sqrt(total_sd**2 - judge_sd**2)``.

MEASURED 2026-09-17, router engine 6134…, the same deterministic 20 cases
(``_select_cases("router_agent", 20)``) every time. Run twice, and the second pass
changed the answer:

    metric                  judge sd (n=3)   judge sd (n=8)   change
    instruction_following            0.086            0.179     2.1x
    response_quality                 0.275            0.171     0.6x
    hallucination                    0.049            0.031     0.6x
    safety                           0.087            0.054     0.6x

Decomposition on the n=8 judge figures (totals are still only n=3 full runs):

    metric                  mean   judge sd   total sd   agent sd   agent %var
    instruction_following   3.50      0.179      0.335      0.283          71%
    response_quality        3.81      0.171      0.120    INCOHERENT         —
    hallucination           4.57      0.031        n/a        n/a           —
    safety                  4.63      0.054        n/a        n/a           —

Three conclusions:

1. ``instruction_following`` is **agent-dominated** — 71% of its variance is the
   router answering differently run to run. Re-running will not stabilise it; only
   the agent can. (The n=3 pass put this at 93%: the direction held, the magnitude
   did not.)
2. ``response_quality`` is **undecidable**, and which side is suspect has flipped.
   At n=3 the judge estimate looked wrong; at n=8 the judge is solid and the TOTAL
   is the n=3 number — and it came out *below* the judge variance it contains,
   which cannot happen. Do not reach for :mod:`src.eval.judge_panel` on this.
3. ``hallucination`` and ``safety`` sit 30-50 sd above their floors. Nearly inert —
   cheap to keep, but do not mistake them for active protection.

Detection limits for a rolling-baseline z >= 2: ``instruction_following`` 0.67,
``response_quality`` >= 0.34, ``hallucination`` 0.06, ``safety`` 0.11.

**The next measurement costs nothing.** What is missing is more FULL runs for the
totals, and the daily ``router_quality`` workflow produces exactly one per day.
Re-decompose after a week instead of paying for fresh inference.

**On sample size.** ``--repeats 3`` is enough to notice that a metric moves; it is
not enough to say by how much. A standard deviation from three observations carries
a 95% interval about twelve times as wide as itself, which is exactly how this
spike produced a judge sd larger than the total it is a component of. Treat n=3 as
a smoke test and use ``--repeats 8`` or more before changing a threshold.

Cost: one inference pass (20 cases) plus N scoring passes. The scoring passes are
the point — the inference is deliberately captured once and reused.

Usage::

    uv run python -m src.eval.spike_metric_noise --agent-id <ENGINE_ID> --repeats 3
    uv run python -m src.eval.spike_metric_noise --dry-run
"""

from __future__ import annotations

import argparse
import math
import statistics
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_REPEATS = 3
DEFAULT_LIMIT = 20
POLL_SECONDS = 900


def scale_to_five(value: float) -> float:
    """0-1 rubric score -> the published 1-5 axis."""
    return value * 4.0 + 1.0


def agent_sd(total_sd: float, judge_sd: float) -> float:
    """Variances add: the agent's share of an observed total spread.

    Clamped at zero — a judge sd measured larger than the total is sampling noise
    on a handful of runs, not a negative variance, and returning ``nan`` here would
    propagate into a report that is otherwise fine.
    """
    return math.sqrt(max(total_sd**2 - judge_sd**2, 0.0))


def summarize(samples: Sequence[float]) -> dict[str, float]:
    """mean / sd / range for one metric's repeated scores."""
    vals = [float(v) for v in samples]
    return {
        "n": len(vals),
        "mean": statistics.mean(vals),
        "sd": statistics.stdev(vals) if len(vals) > 1 else 0.0,
        "range": max(vals) - min(vals) if vals else 0.0,
    }


def detectable_shift(sd: float, z: float = 2.0) -> float:
    """Smallest change a rolling-baseline z-test can call at this noise level."""
    return z * sd


def _score_once(client, inference, agent_resource, metrics, agent_info, label: str) -> dict:
    """One evaluation run over an ALREADY-CAPTURED inference result."""
    from src.config import GCP_STAGING_BUCKET
    from src.eval.eval_experiment import eval_run_display_name, eval_run_labels

    kwargs = {
        "dataset": inference,
        "agent": agent_resource,
        "metrics": metrics,
        "dest": f"gs://{GCP_STAGING_BUCKET}/eval-results/",
        "display_name": eval_run_display_name("router_agent", label),
        "labels": eval_run_labels("router_agent", "batch"),
    }
    if agent_info is not None:
        kwargs["agent_info"] = agent_info

    run = client.evals.create_evaluation_run(**kwargs)
    deadline = time.time() + POLL_SECONDS
    while time.time() < deadline:
        run = client.evals.get_evaluation_run(name=run.name)
        if any(s in str(getattr(run, "state", "")) for s in ("SUCCEEDED", "FAILED", "CANCELLED")):
            break
        time.sleep(15)

    # Summary metrics only — see simulated_eval for why the per-item flag is off.
    run = client.evals.get_evaluation_run(name=run.name)
    summary = getattr(getattr(run, "evaluation_run_results", None), "summary_metrics", None)
    raw = dict(getattr(summary, "metrics", {}) or {})
    return {
        key.rsplit("/AVERAGE", 1)[0].rsplit("/", 1)[-1]: scale_to_five(float(value))
        for key, value in raw.items()
        if "/AVERAGE" in key
    }


def run_judge_noise(agent_id: str, *, repeats: int, limit: int, agent_name: str) -> dict:
    """Capture inference once, score it ``repeats`` times, report the spread."""
    import vertexai
    from agentplatform import Client

    from src.config import GCP_PROJECT_ID, GCP_REGION, GCP_STAGING_BUCKET, disable_pyopenssl
    from src.eval.agent_eval_configs import get_metrics
    from src.eval.batch_eval import _resolve_agent_resource_name
    from src.eval.eval_experiment import ensure_eval_experiment
    from src.eval.multi_agent_batch_eval import (
        _agent_info_for,
        _build_eval_dataset,
        _select_cases,
        count_tool_call_items,
        drop_tool_use_metric_if_unscorable,
        partition_empty_responses,
    )

    vertexai.init(
        project=GCP_PROJECT_ID, location=GCP_REGION, staging_bucket=f"gs://{GCP_STAGING_BUCKET}"
    )
    disable_pyopenssl()
    client = Client(project=GCP_PROJECT_ID, location=GCP_REGION)
    resource = _resolve_agent_resource_name(agent_id)

    cases = _select_cases(agent_name, limit)
    print(f"Inference over {len(cases)} cases — ONCE, then scored {repeats}x")
    inference = client.evals.run_inference(agent=resource, src=_build_eval_dataset(cases))

    kept = partition_empty_responses(inference)
    frame = getattr(inference, "eval_dataset_df", None)
    with_calls, total_items = count_tool_call_items(frame)
    print(f"  scored items={kept}  tool-calling items={with_calls}/{total_items}")

    metrics = drop_tool_use_metric_if_unscorable(get_metrics(agent_name), with_calls, total_items)
    agent_info = _agent_info_for(agent_name, getattr(inference, "candidate_name", None))
    ensure_eval_experiment(client=client)

    passes = []
    for i in range(1, repeats + 1):
        scores = _score_once(client, inference, resource, metrics, agent_info, f"noise{i}")
        passes.append(scores)
        print(f"  pass {i}: " + "  ".join(f"{k}={v:.2f}" for k, v in sorted(scores.items())))

    return {
        name: summarize([p[name] for p in passes if name in p])
        for name in sorted({k for p in passes for k in p})
    }


def render(stats: dict) -> str:
    lines = [
        "",
        "=== JUDGE-ONLY VARIANCE (identical inference, 1-5 scale) ===",
        f"  {'metric':<28} {'mean':>6} {'sd':>7} {'range':>7}  {'z>=2 detects':>13}",
    ]
    for name, s in stats.items():
        lines.append(
            f"  {name:<28} {s['mean']:>6.2f} {s['sd']:>7.3f} {s['range']:>7.2f}"
            f"  {detectable_shift(s['sd']):>13.2f}"
        )
    lines += [
        "",
        "Everything above moved with the INPUT HELD CONSTANT, so it is the judge.",
        "Subtract it from a full-run spread to get the agent's share (agent_sd).",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent-id", help="engine to score (default: ROUTER_ENGINE_ID)")
    parser.add_argument("--agent-name", default="router_agent")
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--dry-run", action="store_true", help="print the cost, contact nothing")
    args = parser.parse_args(argv)

    if args.dry_run:
        print(
            f"DRY RUN — 1 inference pass over {args.limit} cases, then {args.repeats} "
            f"scoring passes over that same capture.\n"
            "  The inference is captured ONCE on purpose: re-running it would mix the "
            "agent's variance back into a judge-only measurement."
        )
        return 0

    from src.config import ROUTER_ENGINE_ID

    agent_id = args.agent_id or ROUTER_ENGINE_ID
    if not agent_id:
        raise SystemExit("no --agent-id and ROUTER_ENGINE_ID is unset")

    stats = run_judge_noise(
        agent_id, repeats=args.repeats, limit=args.limit, agent_name=args.agent_name
    )
    print(render(stats))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
