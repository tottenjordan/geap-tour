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
(``_select_cases("router_agent", 20)``) every time:

    metric                  mean   judge sd   total sd   agent sd   floor headroom
    instruction_following   3.40      0.086      0.335      0.324          2.7 sd
    response_quality        3.74      0.275        n/a        n/a          2.7 sd
    hallucination           4.31      0.049        n/a        n/a         26.6 sd
    safety                  4.85      0.087        n/a        n/a         21.2 sd

Three conclusions that change how these series should be read:

1. ``instruction_following`` is **agent-dominated** — 93% of its variance is the
   router giving genuinely different answers run to run, not the judge wobbling.
   Re-running it will not stabilise it; only the agent can.
2. ``response_quality`` is the **opposite**: the judge alone moved 3.47 -> 4.02 on
   byte-identical input. That is the case a judge panel exists for, and it is not
   wired here (the rubric is scored by the SDK, not by our standalone judges).
3. ``hallucination`` and ``safety`` sit 21-27 sd above their floors. They are
   nearly inert — cheap to keep, but do not mistake them for active protection.

Detection limits that follow, for a rolling-baseline z >= 2:

    instruction_following   needs a shift >= 0.67
    response_quality        needs a shift >= 0.55
    hallucination           needs a shift >= 0.10
    safety                  needs a shift >= 0.17

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

    run = client.evals.get_evaluation_run(name=run.name, include_evaluation_items=True)
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
