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
