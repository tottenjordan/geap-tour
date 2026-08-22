"""Quality alerts — Cloud Monitoring alert policies for evaluation score thresholds."""

from google.cloud import monitoring_v3
from google.protobuf import duration_pb2

from src.config import GCP_PROJECT_ID, RESOURCE_LABELS

# ty's generated protobuf stubs don't expose the well-known Duration message
# (it exists at runtime); alias it once rather than ignoring at each call site.
_Duration = duration_pb2.Duration  # ty: ignore[unresolved-attribute]

_COMPARISONS = {
    "LT": monitoring_v3.ComparisonType.COMPARISON_LT,
    "GT": monitoring_v3.ComparisonType.COMPARISON_GT,
}


def _build_policy(
    metric_name: str,
    threshold: float,
    channels: list[str],
    *,
    comparison: str = "LT",
    family: str = "agent_eval",
) -> monitoring_v3.AlertPolicy:
    """Build the AlertPolicy proto (pure; no API calls) for one monitored metric.

    ``comparison`` is ``"LT"`` (alert when the value falls *below* the threshold —
    the right direction for quality/accuracy/savings metrics) or ``"GT"`` (alert
    when the value rises *above* the threshold — e.g. latency ceilings).
    ``family`` selects the metric namespace (``agent_eval`` for coordinator
    quality, ``agent_router`` for router efficiency).
    """
    direction = "below" if comparison == "LT" else "above"
    condition = monitoring_v3.AlertPolicy.Condition(
        display_name=f"Agent {metric_name} {direction} {threshold}",
        condition_threshold=monitoring_v3.AlertPolicy.Condition.MetricThreshold(
            filter=f'metric.type="custom.googleapis.com/{family}/{metric_name}" AND resource.type="global"',
            comparison=_COMPARISONS[comparison],
            threshold_value=threshold,
            duration=_Duration(seconds=600),
            aggregations=[
                monitoring_v3.Aggregation(
                    alignment_period=_Duration(seconds=600),
                    per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_MEAN,
                )
            ],
        ),
    )

    return monitoring_v3.AlertPolicy(
        display_name=f"GEAP Workshop: {metric_name} quality alert",
        documentation=monitoring_v3.AlertPolicy.Documentation(
            content=f"Agent metric '{metric_name}' moved {direction} {threshold}. "
            "Check recent eval results and agent behavior.",
            mime_type="text/markdown",
        ),
        conditions=[condition],
        combiner=monitoring_v3.AlertPolicy.ConditionCombinerType.OR,
        notification_channels=channels,
        enabled=True,
        user_labels=dict(RESOURCE_LABELS),
    )


def create_quality_alert(
    metric_name: str = "helpfulness",
    threshold: float = 3.0,
    notification_channel: str | None = None,
    *,
    comparison: str = "LT",
    family: str = "agent_eval",
):
    """Create a Cloud Monitoring alert policy for a monitored metric.

    ``comparison``/``family`` are forwarded to :func:`_build_policy` so the same
    call serves coordinator quality (``LT``/``agent_eval``) and router efficiency
    (``agent_router``, with ``GT`` for latency ceilings).
    """
    client = monitoring_v3.AlertPolicyServiceClient()
    project_name = f"projects/{GCP_PROJECT_ID}"

    channels = [notification_channel] if notification_channel else []
    policy = _build_policy(metric_name, threshold, channels, comparison=comparison, family=family)

    result = client.create_alert_policy(name=project_name, alert_policy=policy)
    op = "<" if comparison == "LT" else ">"
    print(f"✓ Alert policy created: {result.name}")
    print(f"  Metric: {family}/{metric_name} {op} {threshold}")
    print("  Window: 10 minutes")
    return result


def list_quality_alerts():
    """List all GEAP workshop alert policies."""
    client = monitoring_v3.AlertPolicyServiceClient()
    project_name = f"projects/{GCP_PROJECT_ID}"

    policies = client.list_alert_policies(name=project_name)
    workshop_policies = [p for p in policies if "GEAP Workshop" in p.display_name]

    if not workshop_policies:
        print("No GEAP workshop alert policies found.")
        return

    print(f"Found {len(workshop_policies)} alert policies:")
    for p in workshop_policies:
        status = "enabled" if p.enabled else "disabled"
        print(f"  - {p.display_name} [{status}]")
        print(f"    {p.name}")


# Coordinator quality series (``agent_eval/*``, 1-5 rubric axis, alert on the
# floor with LT). This is ONLY the coordinator (a task executor) — the router's
# efficiency numbers live on their own series (see ROUTER_MONITORED_METRICS).
ALL_MONITORED_METRICS = [
    ("helpfulness", 3.0),
    ("tool_use_accuracy", 3.0),
    ("policy_compliance", 3.0),
    # Tool-call faithfulness: does the response truthfully reflect the tools that
    # actually executed? A hallucinated-action detector on the same 1-5 floor.
    # Unlike the three above (scored on response text via run_inference), this is
    # scored from the real stream_query trajectory (see src/eval/tool_faithfulness.py).
    ("tool_faithfulness", 3.0),
]

# Router efficiency series (native units, ``agent_router/*``). Unlike coordinator
# quality these are NOT on a 1-5 axis and don't all alert in the same direction:
# routing accuracy / cost savings alert on the FLOOR (LT); classifier latency
# alerts on the CEILING (GT).
#
# Thresholds are data-driven from the router eval set (observed: accuracy
# 92-100%, cost savings 60-63% vs an all-Opus baseline, classifier avg latency
# ~4200ms — the classifier makes a real LLM call to a thinking model, so
# multi-second latency is normal). Chosen with headroom for normal variance so
# alerts page on genuine degradation rather than noise:
#   - routing_accuracy_pct  < 80%    (~12pp below observed; robust to single-
#                                     case flips on a small eval set)
#   - cost_savings_pct      < 50%    (~10pp margin; catches routing drifting
#                                     toward expensive tiers)
#   - classifier_latency_ms > 8000ms (~2x observed avg; catches a real slowdown
#                                     without firing on the normal ~4200ms)
ROUTER_MONITORED_METRICS = [
    ("routing_accuracy_pct", 80.0, "LT"),
    ("cost_savings_pct", 50.0, "LT"),
    ("classifier_latency_ms", 8000.0, "GT"),
]

# Online coordinator quality series (``agent_online_eval/*``). Same rubrics as
# ALL_MONITORED_METRICS on the same 1-5 axis, but a SEPARATE family so continuous,
# client-side-sampled live-traffic scores (``eval_mode=online``) never blur into
# the periodic offline snapshot (``agent_eval/*``, ``eval_mode=offline``). This
# surface is fed by scoring live ``stream_query`` responses captured client-side
# (see src/eval/online_monitor.py).
#
# **The floors are NOT the same as offline, and that is deliberate.** The two
# surfaces score the same rubrics over *different prompt mixes*, so they do not
# share a distribution and never should have shared a number:
#
#   ``ONLINE_PROBE_PROMPTS`` includes policy-*adjacent* tasks — "search hotels
#   under $350", "book a trip and submit my last meal receipt" — where the user
#   never asks about policy. The coordinator's GEPA-optimized instruction surfaces
#   policy status "when an expense submission is requested", i.e. after
#   ``check_expense_policy`` runs, so on those prompts the trigger never fires and
#   the policy judge scores 0.2 for "no awareness of the policy". Measured
#   2026-08-22 over the 6-prompt probe set: the two non-policy prompts scored 1.0,
#   the two policy-adjacent ones 0.2. The agent is following its instruction; the
#   rubric expects proactive disclosure the instruction does not mandate. Reviewed
#   and the CURRENT BEHAVIOUR WAS JUDGED CORRECT — surfacing a limit before an
#   amount exists is noise — so the floor moves, not the agent.
#
# Observed online over 4 hourly runs (n=6 probes each): helpfulness 4.17-4.50,
# tool_use_accuracy 3.00-4.33, policy_compliance 3.00-3.50. The latter two *touched*
# the old 3.0 floor, so a shared floor was one noisy sample away from paging on
# healthy traffic. Floors below sit clear of the observed range:
#   - helpfulness       3.0  (observed min 4.17 — >1.1 headroom; keeps offline parity)
#   - tool_use_accuracy 2.5  (observed min 3.00 — the old floor WAS the minimum)
#   - policy_compliance 2.5  (observed min 3.00; structurally lower, see above)
#   - tool_faithfulness 3.0  (hallucinated-action detector; a real drop must page)
#
# **Provisional — n=4 runs.** Revisit once the rolling baseline in
# ``src/eval/baseline.py`` has real history; its z-score anomaly detection catches
# *changes* regardless of absolute level and is the better long-run signal here.
ONLINE_MONITORED_METRICS = [
    ("helpfulness", 3.0),
    ("tool_use_accuracy", 2.5),
    ("policy_compliance", 2.5),
    ("tool_faithfulness", 3.0),
]

# Online *infra* series (``agent_online_eval/*``, same family as the online quality
# rubrics but a DIFFERENT axis). ``infra_empty_rate`` is a 0-1 rate written
# verbatim (no 1-5 scaling) and alerts on the CEILING (GT): a rising share of
# empty-at-200 / error-shaped responses is an infrastructure failure (cold-start /
# timeout empty streams), NOT a low quality score, so it pages on its own signal
# instead of dragging the helpfulness mean (see memory
# ``online-helpfulness-dips-are-empty-streams``). Threshold 0.2 = alert once more
# than a fifth of sampled live traffic comes back empty.
ONLINE_INFRA_METRICS = [
    ("infra_empty_rate", 0.2, "GT"),
]


# ---------------------------------------------------------------------------
# Managed engine-health alerts — the platform's OWN Reasoning Engine metrics
# ---------------------------------------------------------------------------
# Everything above alerts on our custom.googleapis.com/* gauges (self-reported,
# resource.type="global", and only present while our traffic generator / eval
# publishers run). These two policies instead alert on the AUTHORITATIVE
# server-side metrics the Agent Runtime emits automatically on
# resource.type="aiplatform.googleapis.com/ReasoningEngine": engine request
# latency and 5xx error rate. They page on genuine engine degradation even with
# no client-side instrumentation running, and are grouped by reasoning_engine_id
# so each deployed engine (probe / pinned / bake-off) is evaluated on its own
# series. This mirrors the runtime monitoring doc's alerting example (p99
# request latency > 5s). See docs/notes/runtime-monitoring.md.
ENGINE_RESOURCE_TYPE = "aiplatform.googleapis.com/ReasoningEngine"
ENGINE_LATENCY_METRIC = "aiplatform.googleapis.com/reasoning_engine/request_latencies"
ENGINE_REQUEST_COUNT_METRIC = "aiplatform.googleapis.com/reasoning_engine/request_count"
ENGINE_LATENCY_P99_MS = 5000.0  # request_latencies is milliseconds (doc: "5000ms" = 5s)
ENGINE_ERROR_RATE = 0.05  # alert when >5% of requests return a 5xx over the window


def _engine_scope(engine_id: str | None) -> str:
    """Resource filter for the managed engine metrics, optionally one engine.

    ``engine_id`` may be a bare id or a full ``.../reasoningEngines/<id>`` name;
    only the bare id is used in the ``reasoning_engine_id`` resource label.
    """
    scope = f'resource.type="{ENGINE_RESOURCE_TYPE}"'
    if engine_id:
        bare = engine_id.rsplit("/", 1)[-1]
        scope += f' AND resource.labels.reasoning_engine_id="{bare}"'
    return scope


def _engine_aggregation() -> monitoring_v3.Aggregation:
    """5-min ALIGN_RATE, summed per engine — shared by the error-rate ratio."""
    return monitoring_v3.Aggregation(
        alignment_period=_Duration(seconds=300),
        per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_RATE,
        cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_SUM,
        group_by_fields=["resource.label.reasoning_engine_id"],
    )


def _build_engine_latency_policy(
    threshold_ms: float, channels: list[str], *, engine_id: str | None = None
) -> monitoring_v3.AlertPolicy:
    """Alert when the engine's p99 request latency exceeds ``threshold_ms``."""
    condition = monitoring_v3.AlertPolicy.Condition(
        display_name=f"Engine p99 request latency above {threshold_ms}ms",
        condition_threshold=monitoring_v3.AlertPolicy.Condition.MetricThreshold(
            filter=f'metric.type="{ENGINE_LATENCY_METRIC}" AND {_engine_scope(engine_id)}',
            comparison=_COMPARISONS["GT"],
            threshold_value=threshold_ms,
            duration=_Duration(seconds=300),
            aggregations=[
                monitoring_v3.Aggregation(
                    alignment_period=_Duration(seconds=300),
                    per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_99,
                    cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_MEAN,
                    group_by_fields=["resource.label.reasoning_engine_id"],
                )
            ],
        ),
    )
    return monitoring_v3.AlertPolicy(
        display_name="GEAP Workshop: engine request latency (p99) alert",
        documentation=monitoring_v3.AlertPolicy.Documentation(
            content=(
                f"Reasoning Engine p99 request latency exceeded {threshold_ms}ms over 5 min. "
                "This is the platform's server-side latency (not client-measured) — check "
                "engine health, cold starts, and model-backend latency."
            ),
            mime_type="text/markdown",
        ),
        conditions=[condition],
        combiner=monitoring_v3.AlertPolicy.ConditionCombinerType.OR,
        notification_channels=channels,
        enabled=True,
        user_labels=dict(RESOURCE_LABELS),
    )


def _build_engine_error_rate_policy(
    threshold: float, channels: list[str], *, engine_id: str | None = None
) -> monitoring_v3.AlertPolicy:
    """Alert when the 5xx share of requests exceeds ``threshold`` (a ratio).

    Uses a numerator/denominator MetricThreshold: 5xx request_count rate over
    total request_count rate, so it fires on the *proportion* of failures, not
    an absolute count (robust to traffic volume).
    """
    scope = _engine_scope(engine_id)
    condition = monitoring_v3.AlertPolicy.Condition(
        display_name=f"Engine 5xx error rate above {threshold}",
        condition_threshold=monitoring_v3.AlertPolicy.Condition.MetricThreshold(
            filter=(
                f'metric.type="{ENGINE_REQUEST_COUNT_METRIC}" '
                f'AND metric.labels.response_code_class="5xx" AND {scope}'
            ),
            denominator_filter=f'metric.type="{ENGINE_REQUEST_COUNT_METRIC}" AND {scope}',
            aggregations=[_engine_aggregation()],
            denominator_aggregations=[_engine_aggregation()],
            comparison=_COMPARISONS["GT"],
            threshold_value=threshold,
            duration=_Duration(seconds=300),
        ),
    )
    return monitoring_v3.AlertPolicy(
        display_name="GEAP Workshop: engine 5xx error rate alert",
        documentation=monitoring_v3.AlertPolicy.Documentation(
            content=(
                f"Reasoning Engine 5xx responses exceeded {threshold:.0%} of requests over "
                "5 min (server-side request_count ratio). Check engine logs "
                "(reasoning_engine_stderr) and recent deploys."
            ),
            mime_type="text/markdown",
        ),
        conditions=[condition],
        combiner=monitoring_v3.AlertPolicy.ConditionCombinerType.OR,
        notification_channels=channels,
        enabled=True,
        user_labels=dict(RESOURCE_LABELS),
    )


def create_engine_health_alerts(
    notification_channel: str | None = None, *, engine_id: str | None = None
) -> list:
    """Create alert policies on the platform's managed Reasoning Engine metrics.

    Two policies — p99 request-latency ceiling and 5xx error-rate ceiling — both
    on ``resource.type="aiplatform.googleapis.com/ReasoningEngine"`` and grouped
    per engine id. Unlike the custom-metric alerts these fire on the authoritative
    server-side signal, independent of the client-side traffic generator.
    """
    client = monitoring_v3.AlertPolicyServiceClient()
    project_name = f"projects/{GCP_PROJECT_ID}"
    channels = [notification_channel] if notification_channel else []
    results = []
    for policy in (
        _build_engine_latency_policy(ENGINE_LATENCY_P99_MS, channels, engine_id=engine_id),
        _build_engine_error_rate_policy(ENGINE_ERROR_RATE, channels, engine_id=engine_id),
    ):
        result = client.create_alert_policy(name=project_name, alert_policy=policy)
        print(f"✓ Alert policy created: {result.name}")
        results.append(result)
    return results


def setup_all_alerts(notification_channel: str | None = None) -> list:
    """Create alert policies for every monitored metric (both families)."""
    results = []
    print("Setting up quality alerts for all metrics...")
    for metric_name, threshold in ALL_MONITORED_METRICS:
        try:
            result = create_quality_alert(
                metric_name=metric_name,
                threshold=threshold,
                notification_channel=notification_channel,
            )
            results.append(result)
        except Exception as e:
            print(f"  Warning: failed to create alert for {metric_name}: {e}")
    for metric_name, threshold, comparison in ROUTER_MONITORED_METRICS:
        try:
            result = create_quality_alert(
                metric_name=metric_name,
                threshold=threshold,
                notification_channel=notification_channel,
                comparison=comparison,
                family="agent_router",
            )
            results.append(result)
        except Exception as e:
            print(f"  Warning: failed to create alert for {metric_name}: {e}")
    for metric_name, threshold in ONLINE_MONITORED_METRICS:
        try:
            result = create_quality_alert(
                metric_name=metric_name,
                threshold=threshold,
                notification_channel=notification_channel,
                family="agent_online_eval",
            )
            results.append(result)
        except Exception as e:
            print(f"  Warning: failed to create alert for {metric_name}: {e}")
    for metric_name, threshold, comparison in ONLINE_INFRA_METRICS:
        try:
            result = create_quality_alert(
                metric_name=metric_name,
                threshold=threshold,
                notification_channel=notification_channel,
                comparison=comparison,
                family="agent_online_eval",
            )
            results.append(result)
        except Exception as e:
            print(f"  Warning: failed to create alert for {metric_name}: {e}")
    # Managed engine-health alerts on the platform's own reasoning_engine metrics.
    try:
        results.extend(create_engine_health_alerts(notification_channel))
    except Exception as e:
        print(f"  Warning: failed to create engine-health alerts: {e}")
    print(f"\n  {len(results)} alert policies created")
    return results


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "list":
        list_quality_alerts()
    elif len(sys.argv) > 1 and sys.argv[1] == "all":
        setup_all_alerts()
    else:
        metric = sys.argv[1] if len(sys.argv) > 1 else "helpfulness"
        threshold = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
        create_quality_alert(metric_name=metric, threshold=threshold)
