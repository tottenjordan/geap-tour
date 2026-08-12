"""Bridge offline agent-eval scores onto the ``agent_eval/*`` monitoring series.

The batch / simulated / complexity evals already score the *deployed* engine
via the Vertex Gen AI Evaluation Service (``client.evals.run_inference`` +
``create_evaluation_run``) — Google's canonical "evaluate a deployed agent"
flow — but their scores only ever land in local JSON. This module extracts the
four monitored metrics from those results, scales them onto the 1-5 monitored
scale, and hands them to the shared :func:`publish_eval_metrics` bridge so the
dashboard, alert policies, and ``verify_monitors`` chart real quality.

This is the canonical quality source for the demo: the native Online Evaluators
return ``INSUFFICIENT_DATA`` because the managed Agent Engine runtime does not
emit prompt/response content into the trace/log path (see
``docs``/memory ``online-eval-content-capture-blocked``).

Note on semantics: these are periodic point-in-time snapshots (one write per
eval run), not per-request telemetry — every published point carries an
``eval_mode="offline"`` label so it can be distinguished from any continuous
stream. ``complexity_routing_accuracy`` is a classifier-accuracy fraction scaled
``accuracy * 5`` to sit on the same 1-5 axis as the rubric metrics.
"""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING

from src.eval.publish_eval_metrics import publish_eval_metrics

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from src.observability.metrics import MetricsWriter

DEFAULT_COORDINATOR_AGENT = "coordinator_agent"


def _to_monitored_scale(score: float) -> float:
    """Map a 0-1 eval score onto the 1-5 monitored scale (``0.6 -> 3.0``)."""
    return round(float(score) * 5.0, 3)


def _strip_engine_prefix(key: str) -> str:
    """``agent_engine_0/final_response_quality_v1`` -> ``final_response_quality_v1``."""
    return key.rsplit("/", 1)[-1]


def publish_offline_scores(
    batch_results: Mapping,
    complexity_results: Mapping | None = None,
    coordinator_agent: str = DEFAULT_COORDINATOR_AGENT,
    writer: MetricsWriter | None = None,
    extra_labels: Mapping[str, str] | None = None,
) -> dict[str, float]:
    """Publish offline eval scores to ``agent_eval/*``; return what was written.

    ``batch_results`` is a :func:`run_multi_agent_batch_eval` result dict
    (``{"agents": {name: {"metrics": {key: {"score": 0-1}}}}}``);
    ``complexity_results`` is the ``run_all_evals`` complexity block
    (``{"accuracy": {"accuracy": 0-1}}``). Scores are scaled 0-1 -> 1-5, keyed
    to canonical names, and filtered to ``ALL_MONITORED_METRICS`` by the shared
    :func:`publish_eval_metrics` (so non-monitored rubrics are dropped — no
    metric drift). Returns the exact ``{canonical_name: value}`` emitted.
    """
    metrics = batch_results.get("agents", {}).get(coordinator_agent, {}).get("metrics", {})
    raw: dict[str, float] = {
        _strip_engine_prefix(key): _to_monitored_scale(detail["score"])
        for key, detail in metrics.items()
    }

    if complexity_results:
        accuracy = complexity_results.get("accuracy", {}).get("accuracy")
        if accuracy is not None:
            raw["complexity_routing_accuracy"] = _to_monitored_scale(accuracy)

    labels = {"eval_mode": "offline", **(extra_labels or {})}
    return publish_eval_metrics(raw, writer=writer, extra_labels=labels)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
class _NoopMetricClient:
    """Swallow ``create_time_series`` — used for ``--dry-run`` (no GCP)."""

    def create_time_series(self, name=None, time_series=None):
        return None


def _load_results(path: str) -> tuple[dict, dict | None]:
    """Load a bridge input file → ``(batch_results, complexity_results)``.

    Accepts either a ``run_all_evals`` ``full_results.json`` (has ``batch`` /
    ``complexity`` keys) or a raw ``batch_results_*.json`` (already the batch
    dict, identified by a top-level ``agents`` key). Complexity is ``None`` for
    the raw-batch shape.
    """
    with open(path) as f:
        data = json.load(f)
    if "batch" in data:
        return data["batch"], data.get("complexity")
    return data, None


def _resolve_latest() -> str:
    """Newest ``EVAL_OUTPUT_DIR/*/full_results.json`` by mtime."""
    from pathlib import Path

    from src.config import EVAL_OUTPUT_DIR

    candidates = sorted(
        Path(EVAL_OUTPUT_DIR).glob("*/full_results.json"),
        key=lambda p: p.stat().st_mtime,
    )
    if not candidates:
        raise SystemExit(f"No run_*/full_results.json under {EVAL_OUTPUT_DIR}")
    return str(candidates[-1])


def _inject_policy_compliance(batch: dict) -> None:
    """Score policy_compliance via the standalone judge and add it to ``batch``.

    The custom ``client.evals`` policy metric is SDK-broken (see
    :mod:`src.eval.policy_judge`), so we score it directly and splice the result
    into the coordinator's metrics under the same key shape the bridge expects
    (``agent_engine_0/policy_compliance`` → ``{"score": 0-1}``). Guarded: a
    failure just leaves policy_compliance absent (the bridge skips it).
    """
    from src.config import AGENT_ENGINE_ID, GCP_PROJECT_ID, GCP_REGION
    from src.eval.policy_judge import run_policy_compliance_eval

    arn = f"projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/reasoningEngines/{AGENT_ENGINE_ID}"
    result = run_policy_compliance_eval(arn)
    score = result.get("score")
    if score is None:
        print(
            f"  policy_compliance: not scored ({result.get('n_scored')}/{result.get('n_total')} cases)"
        )
        return
    metrics = (
        batch.setdefault("agents", {})
        .setdefault(DEFAULT_COORDINATOR_AGENT, {})
        .setdefault("metrics", {})
    )
    metrics["agent_engine_0/policy_compliance"] = {"score": score}
    print(f"  policy_compliance: {score:.3f} (over {result.get('n_scored')} responses)")


def _run_fresh() -> tuple[dict, dict]:
    """Run a fresh coordinator batch eval + complexity accuracy eval."""
    import asyncio

    from src.eval.agent_eval_configs import ROUTER_EVAL_CASES
    from src.eval.complexity_metrics import run_complexity_accuracy_eval
    from src.eval.multi_agent_batch_eval import run_multi_agent_batch_eval

    batch = run_multi_agent_batch_eval(agents=[DEFAULT_COORDINATOR_AGENT])
    try:
        _inject_policy_compliance(batch)
    except Exception as e:  # policy scoring is best-effort
        print(f"  policy_compliance scoring failed: {e}")
    accuracy = asyncio.run(run_complexity_accuracy_eval(ROUTER_EVAL_CASES))
    return batch, {"accuracy": accuracy}


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the offline-eval → ``agent_eval/*`` bridge."""
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group()
    src.add_argument(
        "--from-json",
        metavar="PATH",
        help="load a run_all_evals full_results.json or a raw batch_results_*.json",
    )
    src.add_argument(
        "--latest",
        action="store_true",
        help="use the newest EVAL_OUTPUT_DIR/*/full_results.json",
    )
    src.add_argument(
        "--run",
        action="store_true",
        help="run a fresh coordinator batch + complexity eval (one run_inference)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute and print scores without writing to Cloud Monitoring",
    )
    args = parser.parse_args(argv)

    if args.run:
        batch, complexity = _run_fresh()
    else:
        path = _resolve_latest() if args.latest else args.from_json
        if not path:
            parser.error("one of --from-json, --latest, or --run is required")
        batch, complexity = _load_results(path)

    writer = None
    if args.dry_run:
        from src.observability.metrics import MetricsWriter

        writer = MetricsWriter(client=_NoopMetricClient())

    published = publish_offline_scores(batch, complexity_results=complexity, writer=writer)
    prefix = "[dry-run] would publish" if args.dry_run else "published"
    print(f"{prefix}: {json.dumps(published, indent=2, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
