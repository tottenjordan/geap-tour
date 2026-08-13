"""Alert policies carry the default resource label (user_labels)."""

import src.config as cfg
from src.eval.quality_alerts import _build_policy


def test_alert_policy_has_resource_labels():
    p = _build_policy("helpfulness", 3.0, [])
    assert dict(p.user_labels) == cfg.RESOURCE_LABELS


def test_alert_policy_carries_metric_and_threshold():
    p = _build_policy("tool_use_accuracy", 3.0, [])
    assert "tool_use_accuracy" in p.display_name
    assert p.conditions[0].condition_threshold.threshold_value == 3.0


def test_coordinator_policy_defaults_to_lt_and_agent_eval_family():
    from google.cloud import monitoring_v3

    p = _build_policy("helpfulness", 3.0, [])
    cond = p.conditions[0].condition_threshold
    assert cond.comparison == monitoring_v3.ComparisonType.COMPARISON_LT
    assert "custom.googleapis.com/agent_eval/helpfulness" in cond.filter
    assert "below" in p.documentation.content


def test_router_policy_uses_gt_and_router_family():
    from google.cloud import monitoring_v3

    p = _build_policy(
        "classifier_latency_ms",
        2000.0,
        [],
        comparison="GT",
        family="agent_router",
    )
    cond = p.conditions[0].condition_threshold
    assert cond.comparison == monitoring_v3.ComparisonType.COMPARISON_GT
    assert "custom.googleapis.com/agent_router/classifier_latency_ms" in cond.filter
    assert "above" in p.documentation.content


def test_router_monitored_metrics_shape():
    from src.eval.quality_alerts import ROUTER_MONITORED_METRICS

    names = {m[0] for m in ROUTER_MONITORED_METRICS}
    assert names == {"routing_accuracy_pct", "cost_savings_pct", "classifier_latency_ms"}
    # Every entry is (name, threshold, comparison) with a valid direction.
    for _name, threshold, comparison in ROUTER_MONITORED_METRICS:
        assert isinstance(threshold, float)
        assert comparison in {"LT", "GT"}
    # Latency alerts on the ceiling (GT); accuracy/savings alert on the floor (LT).
    by_name = {m[0]: m[2] for m in ROUTER_MONITORED_METRICS}
    assert by_name["classifier_latency_ms"] == "GT"
    assert by_name["routing_accuracy_pct"] == "LT"
    assert by_name["cost_savings_pct"] == "LT"


def test_setup_all_alerts_covers_both_families(monkeypatch):
    from src.eval import quality_alerts as qa

    calls = []

    def _fake_create(metric_name, threshold, notification_channel=None, **kwargs):
        calls.append(
            (
                metric_name,
                threshold,
                kwargs.get("comparison", "LT"),
                kwargs.get("family", "agent_eval"),
            )
        )
        return object()

    monkeypatch.setattr(qa, "create_quality_alert", _fake_create)
    qa.setup_all_alerts()

    families = {c[3] for c in calls}
    assert families == {"agent_eval", "agent_router"}
    # Coordinator metrics keep LT/agent_eval; router latency uses GT/agent_router.
    router_latency = [c for c in calls if c[0] == "classifier_latency_ms"]
    assert router_latency and router_latency[0][2] == "GT"
    assert router_latency[0][3] == "agent_router"
