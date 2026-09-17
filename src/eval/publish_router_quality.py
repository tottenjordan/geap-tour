"""Bridge router *answer quality* onto the ``agent_router_quality/*`` series.

Every router series before this measured the **decision** and none measured the
**answer**: ``classifier_accuracy_pct`` (did the classifier score the prompt into
the right band), ``cost_savings_pct`` (was it cheap), ``classifier_latency_ms`` (was
it fast), and now ``lite_tier_pct`` / ``tiers_used`` (did it keep routing at all).
A router that routes perfectly, cheaply, quickly — and answers badly — moves none
of them.

The coordinator has ``agent_eval/*`` for exactly this. The router had nothing, and
not for want of machinery: ``get_eval_cases("router_agent")`` returns **40 cases**,
``get_metrics("router_agent")`` returns the six rubrics, and
``multi_agent_batch_eval`` already resolves ``router_agent`` to
``ROUTER_ENGINE_ID``. The scoring worked; nobody published it.

**Scale and axis.** Scores arrive 0-1 from the eval service and are published 1-5,
the same transform ``publish_offline_eval`` applies, so a router quality number is
read on the same axis as a coordinator one. They land on a *separate family*
(``agent_router_quality/*``) because ``agent_router/*`` holds percents and
milliseconds — mixing axes in one family is how a dashboard ends up averaging a
latency into a score.

**``tool_use`` is deliberately not published.** The batch eval scores it with the
generic ``TOOL_USE_QUALITY`` rubric, a confirmed false-negative for a domain router
(``docs/notes/coordinator-tool-use-quality.md``). The router's 2026-08-20
rearchitecture to direct tools may have invalidated that finding — but "may have"
is not a basis for an alerting series. Verify it, then add it.

**Cost.** Unlike ``publish_router_efficiency`` (classifier-only, no engine), this
runs a real batch eval against the router engine. ``--limit`` bounds it; there is
no hourly cadence here by design.

Usage::

    uv run python -m src.eval.publish_router_quality --dry-run --limit 8
    uv run python -m src.eval.publish_router_quality --run --limit 8
    uv run python -m src.eval.publish_router_quality --from-json <full_results.json>
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping  # runtime use: isinstance in _score_of
from typing import TYPE_CHECKING

from src.observability.metrics import MetricsWriter, write_router_quality_scores

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Batch-eval metric base names -> the published series name. Only these four are
#: monitored; see the module docstring on ``tool_use``.
METRIC_ALIASES: dict[str, str] = {
    "final_response_quality": "response_quality",
    "response_quality": "response_quality",
    "hallucination": "hallucination",
    "safety": "safety",
    "instruction_following": "instruction_following",
}


def extract_router_quality(batch_result: Mapping | None) -> dict[str, float]:
    """Pull the monitored 1-5 scores out of a ``run_agent_eval`` result.

    Handles the unstable candidate prefix (``runtime_0/`` on aiplatform 2.x,
    ``agent_engine_0/`` on 1.x) and the ``_v1`` suffix via
    :func:`src.doe.harvest._metric_base`, the same normalizer the DOE harvest uses.

    **Publish on the presence of metrics, never on the status string.**
    ``_run_single_agent_eval`` overloads ``"FAILED"``: it means *the eval run broke*
    at one return (an ``error`` key, no metrics) and *the scores were below
    threshold* at another (``"PASSED" if all_pass else "FAILED"`` — metrics
    present). Keying on the string suppressed the second case, which is precisely
    the result this whole series exists to alert on: the router's
    ``instruction_following`` measured 0.43 on a real run, and a status-keyed guard
    published nothing at all. A quality series that silently drops its bad points
    only ever shows good news.

    What genuinely must not publish: a ``SKIPPED`` run (every response empty — infra,
    not quality) and a broken run (``error`` present). Writing a 0 for either would
    render as catastrophic quality on the dashboard.
    """
    from src.doe.harvest import _metric_base
    from src.eval.quality_alerts import ROUTER_QUALITY_MONITORED_METRICS

    if not batch_result:
        return {}
    if batch_result.get("status") == "SKIPPED" or batch_result.get("error"):
        return {}

    monitored = {name for name, _t in ROUTER_QUALITY_MONITORED_METRICS}
    scores: dict[str, float] = {}
    for key, value in (batch_result.get("metrics") or {}).items():
        if value is None:
            continue
        series = METRIC_ALIASES.get(_metric_base(str(key)))
        if series not in monitored:
            continue
        raw = _score_of(value)
        if raw is None:
            continue
        # 0-1 -> 1-5, matching publish_offline_eval's transform exactly.
        scores[series] = round(1.0 + raw * 4.0, 2)
    return scores


def _score_of(value) -> float | None:
    """The numeric score, whether the batch stored a scalar or a detail dict.

    ``_run_single_agent_eval`` stores ``{"score", "threshold", "passed"}`` per
    metric, not a bare float — a detail worth handling rather than assuming, since
    a unit test built on the assumed scalar shape passed happily while the real
    call raised ``TypeError: float() argument must be ... not 'dict'`` on the first
    live run.
    """
    if isinstance(value, Mapping):
        value = value.get("score")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def publish_router_quality(
    batch_result: Mapping | None,
    writer: MetricsWriter | None = None,
    extra_labels: Mapping[str, str] | None = None,
) -> dict[str, float]:
    """Publish router quality to ``agent_router_quality/*``; return what was written."""
    scores = extract_router_quality(batch_result)
    if not scores:
        return {}
    labels = {"eval_mode": "offline", "surface": "router", **(extra_labels or {})}
    write_router_quality_scores(scores, writer=writer, extra_labels=labels)
    return scores


def run_batch(limit: int | None = None, agent_id: str | None = None) -> dict:
    """Score the router with the existing batch eval. Costs engine calls."""
    import vertexai
    from agentplatform import Client

    from src.config import GCP_PROJECT_ID, GCP_REGION, ROUTER_ENGINE_ID
    from src.eval.batch_eval import _resolve_agent_resource_name
    from src.eval.multi_agent_batch_eval import _run_single_agent_eval

    engine = agent_id or ROUTER_ENGINE_ID
    if not engine:
        raise SystemExit("ROUTER_ENGINE_ID is unset and --agent-id was not given.")

    # `agent_engines.get` needs region init before the resource resolves.
    vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
    return _run_single_agent_eval(
        Client(project=GCP_PROJECT_ID, location=GCP_REGION),
        "router_agent",
        _resolve_agent_resource_name(engine),
        score_threshold=3.0,
        limit=limit,
    )


class _NoopMetricClient:
    """Swallow ``create_time_series`` — used for ``--dry-run`` (no GCP writes)."""

    def create_time_series(self, name=None, time_series=None):
        return None


def _load_batch(path: str) -> dict:
    """Read a ``run_all_evals`` artifact and find the router's block."""
    with open(path) as fh:
        data = json.load(fh)
    agents = data.get("agents", data)
    if isinstance(agents, dict) and "router_agent" in agents:
        return agents["router_agent"]
    if isinstance(agents, list):
        for entry in agents:
            if isinstance(entry, dict) and entry.get("agent") == "router_agent":
                return entry
    return data if data.get("agent") == "router_agent" else {}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--run", action="store_true", help="score the router now (engine calls)")
    source.add_argument("--from-json", metavar="PATH", help="read a run_all_evals artifact")
    parser.add_argument("--agent-id", help="override ROUTER_ENGINE_ID")
    parser.add_argument("--limit", type=int, help="cap the number of cases scored")
    parser.add_argument("--dry-run", action="store_true", help="compute but do not write")
    parser.add_argument("--label", action="append", metavar="KEY=VALUE")
    args = parser.parse_args(argv)

    batch = run_batch(args.limit, args.agent_id) if args.run else _load_batch(args.from_json)

    from src.observability.metrics import parse_labels

    writer = MetricsWriter(client=_NoopMetricClient()) if args.dry_run else None
    published = publish_router_quality(batch, writer=writer, extra_labels=parse_labels(args.label))

    prefix = "[dry-run] would publish" if args.dry_run else "published"
    print(f"{prefix}: {json.dumps(published, indent=2, sort_keys=True)}")
    if not published:
        # Not a pass. A batch that scored nothing is an absent measurement, and the
        # exit code has to say so or a broken run reads as a healthy one.
        status = (batch or {}).get("status", "no metrics")
        print(f"  NOTHING PUBLISHED — batch status: {status}")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
