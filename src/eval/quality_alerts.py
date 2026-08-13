"""Quality alerts — Cloud Monitoring alert policies for evaluation score thresholds."""

from google.cloud import monitoring_v3
from google.protobuf import duration_pb2

from src.config import GCP_PROJECT_ID, RESOURCE_LABELS

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
            duration=duration_pb2.Duration(seconds=600),
            aggregations=[
                monitoring_v3.Aggregation(
                    alignment_period=duration_pb2.Duration(seconds=600),
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
