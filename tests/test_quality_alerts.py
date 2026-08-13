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
