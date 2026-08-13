"""Bridge offline *coordinator* eval scores onto the ``agent_eval/*`` series.

The coordinator is a task executor: its success is output quality, so its
monitored metrics are LLM rubrics on a 1-5 axis. The batch eval already scores
the *deployed* engine via the Vertex Gen AI Evaluation Service
(``client.evals.run_inference`` + ``create_evaluation_run``) — Google's
canonical "evaluate a deployed agent" flow — but the scores only ever land in
local JSON. This module extracts the coordinator's monitored metrics, scales
them onto the 1-5 monitored axis, and hands them to the shared
:func:`publish_eval_metrics` bridge so the dashboard, alert policies, and
``verify_monitors`` chart real quality.

This is the canonical quality source for the demo: the native Online Evaluators
return ``INSUFFICIENT_DATA`` because the managed Agent Engine runtime does not
emit prompt/response content into the trace/log path (see
``docs``/memory ``online-eval-content-capture-blocked``).

The router is a separate surface entirely (an economic optimizer, not a task
executor): its efficiency numbers publish to ``agent_router/*`` in native units
via :mod:`src.eval.publish_router_efficiency`, NOT here.

Note on semantics: these are periodic point-in-time snapshots (one write per
eval run), not per-request telemetry — every published point carries an
``eval_mode="offline"`` label so it can be distinguished from any continuous
stream.
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
    coordinator_agent: str = DEFAULT_COORDINATOR_AGENT,
    writer: MetricsWriter | None = None,
    extra_labels: Mapping[str, str] | None = None,
) -> dict[str, float]:
    """Publish offline coordinator eval scores to ``agent_eval/*``; return what was written.

    ``batch_results`` is a :func:`run_multi_agent_batch_eval` result dict
    (``{"agents": {name: {"metrics": {key: {"score": 0-1}}}}}``). Scores are
    scaled 0-1 -> 1-5, keyed to canonical names, and filtered to
    ``ALL_MONITORED_METRICS`` by the shared :func:`publish_eval_metrics` (so
    non-monitored rubrics are dropped — no metric drift). Returns the exact
    ``{canonical_name: value}`` emitted.
    """
    metrics = batch_results.get("agents", {}).get(coordinator_agent, {}).get("metrics", {})
    raw: dict[str, float] = {
        _strip_engine_prefix(key): _to_monitored_scale(detail["score"])
        for key, detail in metrics.items()
    }

    labels = {"eval_mode": "offline", **(extra_labels or {})}
    return publish_eval_metrics(raw, writer=writer, extra_labels=labels)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
class _NoopMetricClient:
    """Swallow ``create_time_series`` — used for ``--dry-run`` (no GCP)."""

    def create_time_series(self, name=None, time_series=None):
        return None


def _load_results(path: str) -> dict:
    """Load a bridge input file → the coordinator ``batch_results`` dict.

    Accepts either a ``run_all_evals`` ``full_results.json`` (has a ``batch``
    key) or a raw ``batch_results_*.json`` (already the batch dict, identified by
    a top-level ``agents`` key).
    """
    with open(path) as f:
        data = json.load(f)
    if "batch" in data:
        return data["batch"]
    return data


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


def _inject_policy_compliance(batch: dict, agent_id: str | None = None) -> None:
    """Score policy_compliance via the standalone judge and add it to ``batch``.

    The custom ``client.evals`` policy metric is SDK-broken (see
    :mod:`src.eval.policy_judge`), so we score it directly and splice the result
    into the coordinator's metrics under the same key shape the bridge expects
    (``agent_engine_0/policy_compliance`` → ``{"score": 0-1}``). Guarded: a
    failure just leaves policy_compliance absent (the bridge skips it).

    ``agent_id`` selects the engine under test (a bare id or a full resource
    name); it defaults to the ``AGENT_ENGINE_ID`` env only when unset. Passing it
    explicitly is what keeps a bake-off's per-deployment policy score bound to the
    right engine rather than the .env default.
    """
    from src.config import AGENT_ENGINE_ID
    from src.eval.batch_eval import _resolve_agent_resource_name
    from src.eval.policy_judge import run_policy_compliance_eval

    arn = _resolve_agent_resource_name(agent_id or AGENT_ENGINE_ID)
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


def _run_fresh(agent_id: str | None = None) -> dict:
    """Run a fresh coordinator batch eval (plus the standalone policy judge).

    ``agent_id`` targets a specific deployment (bake-off); when unset the batch
    eval and policy judge both use the ``AGENT_ENGINE_ID`` env default.
    """
    from src.config import AGENT_ENGINE_ID
    from src.eval.multi_agent_batch_eval import run_multi_agent_batch_eval

    batch = run_multi_agent_batch_eval(
        agents=[DEFAULT_COORDINATOR_AGENT], agent_id=agent_id or AGENT_ENGINE_ID
    )
    try:
        _inject_policy_compliance(batch, agent_id=agent_id)
    except Exception as e:  # policy scoring is best-effort
        print(f"  policy_compliance scoring failed: {e}")
    return batch


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
        help="run a fresh coordinator batch eval (one run_inference)",
    )
    parser.add_argument(
        "--agent-id",
        default=None,
        help="engine (bare id or full resource name) to score with --run; "
        "targets a specific bake-off deployment instead of the AGENT_ENGINE_ID default",
    )
    parser.add_argument(
        "--label",
        action="append",
        metavar="KEY=VALUE",
        help="extra label stamped on every published series (repeatable; e.g. "
        "--label model=claude-sonnet-5 keeps a bake-off's snapshots separable)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute and print scores without writing to Cloud Monitoring",
    )
    args = parser.parse_args(argv)

    if args.run:
        batch = _run_fresh(agent_id=args.agent_id)
    else:
        path = _resolve_latest() if args.latest else args.from_json
        if not path:
            parser.error("one of --from-json, --latest, or --run is required")
        batch = _load_results(path)

    writer = None
    if args.dry_run:
        from src.observability.metrics import MetricsWriter

        writer = MetricsWriter(client=_NoopMetricClient())

    from src.observability.metrics import parse_labels

    published = publish_offline_scores(
        batch, writer=writer, extra_labels=parse_labels(args.label)
    )
    prefix = "[dry-run] would publish" if args.dry_run else "published"
    print(f"{prefix}: {json.dumps(published, indent=2, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
