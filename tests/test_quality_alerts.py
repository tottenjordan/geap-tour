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


def test_online_monitored_metrics_shape():
    from src.eval.quality_alerts import ALL_MONITORED_METRICS, ONLINE_MONITORED_METRICS

    # The online quality surface mirrors the coordinator's three rubrics on the
    # same 1-5 axis, so it alerts on the same floor (3.0) — only the metric
    # family (agent_online_eval) differs.
    assert {m[0] for m in ONLINE_MONITORED_METRICS} == {m[0] for m in ALL_MONITORED_METRICS}
    for _name, threshold in ONLINE_MONITORED_METRICS:
        assert threshold == 3.0


def test_all_monitored_includes_tool_faithfulness():
    from src.eval.quality_alerts import ALL_MONITORED_METRICS, ONLINE_MONITORED_METRICS

    # Faithfulness is a coordinator-quality series on the same 1-5 floor, present
    # on both the offline (agent_eval) and online (agent_online_eval) surfaces.
    assert ("tool_faithfulness", 3.0) in ALL_MONITORED_METRICS
    assert ("tool_faithfulness", 3.0) in ONLINE_MONITORED_METRICS


def test_setup_all_alerts_covers_all_families(monkeypatch):
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

    engine_health_calls = []

    def _fake_engine_health(notification_channel=None, **kwargs):
        engine_health_calls.append(notification_channel)
        return [object(), object()]

    monkeypatch.setattr(qa, "create_quality_alert", _fake_create)
    monkeypatch.setattr(qa, "create_engine_health_alerts", _fake_engine_health)
    results = qa.setup_all_alerts()

    families = {c[3] for c in calls}
    assert families == {"agent_eval", "agent_router", "agent_online_eval"}
    # Coordinator metrics keep LT/agent_eval; router latency uses GT/agent_router.
    router_latency = [c for c in calls if c[0] == "classifier_latency_ms"]
    assert router_latency and router_latency[0][2] == "GT"
    assert router_latency[0][3] == "agent_router"
    # Online family splits by axis: the 1-5 quality rubrics alert on the floor
    # (LT), while the infra_empty_rate ceiling alerts on a spike (GT) — an
    # empty-at-200 surge is an infra failure, not a low quality score.
    online = [c for c in calls if c[3] == "agent_online_eval"]
    online_quality = [c for c in online if c[2] == "LT"]
    online_infra = [c for c in online if c[2] == "GT"]
    assert {c[0] for c in online_quality} == {
        "helpfulness",
        "tool_use_accuracy",
        "policy_compliance",
        "tool_faithfulness",
    }
    assert {c[0] for c in online_infra} == {"infra_empty_rate"}
    # The managed engine-health alerts are created once (its two policies added
    # to the result set), on the platform's own metrics — not via the custom
    # create_quality_alert path.
    assert engine_health_calls == [None]
    assert len(results) == len(calls) + 2


def test_engine_latency_policy_targets_managed_percentile_metric():
    from google.cloud import monitoring_v3

    from src.eval.quality_alerts import ENGINE_LATENCY_P99_MS, _build_engine_latency_policy

    p = _build_engine_latency_policy(ENGINE_LATENCY_P99_MS, [])
    cond = p.conditions[0].condition_threshold
    # Managed resource + metric — NOT the custom resource.type="global" gauges.
    assert 'resource.type="aiplatform.googleapis.com/ReasoningEngine"' in cond.filter
    assert "reasoning_engine/request_latencies" in cond.filter
    assert 'resource.type="global"' not in cond.filter
    # p99 latency ceiling (GT) in milliseconds.
    assert cond.comparison == monitoring_v3.ComparisonType.COMPARISON_GT
    assert cond.threshold_value == ENGINE_LATENCY_P99_MS
    assert (
        cond.aggregations[0].per_series_aligner
        == monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_99
    )
    # Grouped per engine so each deployment is evaluated on its own series.
    assert "resource.label.reasoning_engine_id" in cond.aggregations[0].group_by_fields
    assert dict(p.user_labels)


def test_engine_error_rate_policy_is_a_ratio_on_5xx():
    from google.cloud import monitoring_v3

    from src.eval.quality_alerts import ENGINE_ERROR_RATE, _build_engine_error_rate_policy

    p = _build_engine_error_rate_policy(ENGINE_ERROR_RATE, [])
    cond = p.conditions[0].condition_threshold
    # Numerator filters to 5xx; denominator is all requests → a proportion.
    assert 'metric.labels.response_code_class="5xx"' in cond.filter
    assert "reasoning_engine/request_count" in cond.filter
    assert cond.denominator_filter
    assert "reasoning_engine/request_count" in cond.denominator_filter
    assert 'response_code_class="5xx"' not in cond.denominator_filter
    # Rate aligners on both numerator and denominator; GT on the ratio ceiling.
    assert cond.aggregations[0].per_series_aligner == monitoring_v3.Aggregation.Aligner.ALIGN_RATE
    assert (
        cond.denominator_aggregations[0].per_series_aligner
        == monitoring_v3.Aggregation.Aligner.ALIGN_RATE
    )
    assert cond.comparison == monitoring_v3.ComparisonType.COMPARISON_GT
    assert cond.threshold_value == ENGINE_ERROR_RATE


def test_engine_policies_can_scope_to_one_engine():
    from src.eval.quality_alerts import _build_engine_latency_policy

    # A full resource name is accepted; only the bare id lands in the filter.
    p = _build_engine_latency_policy(
        5000.0, [], engine_id="projects/p/locations/us-central1/reasoningEngines/12345"
    )
    cond = p.conditions[0].condition_threshold
    assert 'resource.labels.reasoning_engine_id="12345"' in cond.filter
