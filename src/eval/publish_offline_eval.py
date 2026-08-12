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

from typing import TYPE_CHECKING

from src.eval.publish_eval_metrics import publish_eval_metrics

if TYPE_CHECKING:
    from collections.abc import Mapping

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
