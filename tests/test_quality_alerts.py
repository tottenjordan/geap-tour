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
    assert names == {"classifier_accuracy_pct", "cost_savings_pct", "classifier_latency_ms"}
    # Every entry is (name, threshold, comparison) with a valid direction.
    for _name, threshold, comparison in ROUTER_MONITORED_METRICS:
        assert isinstance(threshold, float)
        assert comparison in {"LT", "GT"}
    # Latency alerts on the ceiling (GT); accuracy/savings alert on the floor (LT).
    by_name = {m[0]: m[2] for m in ROUTER_MONITORED_METRICS}
    assert by_name["classifier_latency_ms"] == "GT"
    assert by_name["classifier_accuracy_pct"] == "LT"
    assert by_name["cost_savings_pct"] == "LT"


def test_online_monitored_metrics_shape():
    from src.eval.quality_alerts import ALL_MONITORED_METRICS, ONLINE_MONITORED_METRICS

    # Same rubrics as the offline surface, on the same 1-5 axis — only the metric
    # family (agent_online_eval) and the FLOORS differ. See below for why.
    assert {m[0] for m in ONLINE_MONITORED_METRICS} == {m[0] for m in ALL_MONITORED_METRICS}
    for _name, threshold in ONLINE_MONITORED_METRICS:
        assert 1.0 <= threshold <= 5.0, "floors live on the rubric's 1-5 axis"


def test_online_floors_sit_below_the_observed_online_range():
    """Online and offline score the same rubrics over DIFFERENT prompt mixes, so
    they don't share a distribution and must not share a floor.

    ONLINE_PROBE_PROMPTS includes policy-*adjacent* tasks where the user never
    asks about policy; the coordinator only surfaces policy status at submission
    time, so the judge scores those 0.2. Measured over 4 hourly runs, online
    tool_use_accuracy and policy_compliance both *touched* 3.00 — the old floor
    was the observed minimum, i.e. one noisy sample from paging on healthy
    traffic. These floors sit clear of that range.
    """
    from src.eval.quality_alerts import ALL_MONITORED_METRICS, ONLINE_MONITORED_METRICS

    online = dict(ONLINE_MONITORED_METRICS)
    offline = dict(ALL_MONITORED_METRICS)

    assert online["policy_compliance"] < offline["policy_compliance"]
    assert online["tool_use_accuracy"] < offline["tool_use_accuracy"]
    # Both observed a minimum of 3.00; a floor at or above that pages on noise.
    assert online["policy_compliance"] < 3.0
    assert online["tool_use_accuracy"] < 3.0
    # Not loosened into uselessness — still well inside the rubric's range.
    assert online["policy_compliance"] >= 2.0
    assert online["tool_use_accuracy"] >= 2.0


def test_metrics_with_headroom_keep_the_offline_floor():
    """Only the two metrics the probe mix actually depresses were moved.

    helpfulness never dropped below 4.17 online, and tool_faithfulness is the
    hallucinated-action detector — blanket-loosening either would trade a real
    false-page problem for a real missed-page one.
    """
    from src.eval.quality_alerts import ONLINE_MONITORED_METRICS

    online = dict(ONLINE_MONITORED_METRICS)
    assert online["helpfulness"] == 3.0
    assert online["tool_faithfulness"] == 3.0


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


class TestPolicyIdentityIsTheMetricNotTheName:
    """Three duplicate policies accumulated because nothing deduplicated.

    `create_quality_alert` called `create_alert_policy` unconditionally, so every
    run of `quality_alerts all` added another full set — and each duplicate pages
    independently. By 2026-09-08 `agent_eval` helpfulness, policy_compliance and
    tool_use_accuracy each had two identical policies (created 2026-08-12,
    re-created 2026-08-17): same threshold, comparison, duration and aligner.

    The trap in the fix: display names collided across families, so a name-keyed
    dedupe would delete a LIVE online alert as a "duplicate" of the offline one.
    """

    def test_offline_and_online_are_not_confusable_by_name(self):
        from src.eval.quality_alerts import _build_policy

        offline = _build_policy("helpfulness", 3.0, [], family="agent_eval")
        online = _build_policy("helpfulness", 3.0, [], family="agent_online_eval")
        assert offline.display_name != online.display_name

    def test_a_policy_is_identified_by_its_metric_filter(self):
        from src.eval.quality_alerts import _build_policy, find_policies_for_metric

        offline = _build_policy("helpfulness", 3.0, [], family="agent_eval")
        online = _build_policy("helpfulness", 3.0, [], family="agent_online_eval")
        pool = [offline, online]

        assert find_policies_for_metric(pool, "agent_eval", "helpfulness") == [offline]
        assert find_policies_for_metric(pool, "agent_online_eval", "helpfulness") == [online]

    def test_an_unwatched_metric_finds_nothing(self):
        from src.eval.quality_alerts import _build_policy, find_policies_for_metric

        pool = [_build_policy("helpfulness", 3.0, [], family="agent_eval")]
        assert find_policies_for_metric(pool, "agent_eval", "policy_compliance") == []


class TestDuplicateDetection:
    def test_two_policies_on_one_metric_are_reported(self):
        from src.eval.quality_alerts import _build_policy, find_duplicate_alerts

        a = _build_policy("helpfulness", 3.0, [], family="agent_eval")
        b = _build_policy("helpfulness", 3.0, [], family="agent_eval")
        dupes = find_duplicate_alerts([a, b])
        assert len(dupes) == 1
        assert len(next(iter(dupes.values()))) == 2

    def test_the_offline_online_pair_is_NOT_a_duplicate(self):
        """The false positive that would delete real coverage: same rubric, same
        threshold, different series."""
        from src.eval.quality_alerts import _build_policy, find_duplicate_alerts

        offline = _build_policy("helpfulness", 3.0, [], family="agent_eval")
        online = _build_policy("helpfulness", 3.0, [], family="agent_online_eval")
        assert find_duplicate_alerts([offline, online]) == {}

    def test_a_healthy_fleet_reports_nothing(self):
        from src.eval.quality_alerts import (
            ALL_MONITORED_METRICS,
            _build_policy,
            find_duplicate_alerts,
        )

        one_each = [_build_policy(m, t, []) for m, t in ALL_MONITORED_METRICS]
        assert find_duplicate_alerts(one_each) == {}

    def test_it_keeps_exactly_one_survivor_per_metric(self):
        from src.eval.quality_alerts import _build_policy, find_duplicate_alerts

        group = [_build_policy("helpfulness", 3.0, [], family="agent_eval") for _ in range(3)]
        (survivors,) = find_duplicate_alerts(group).values()
        assert len(survivors) == 3, "all three are returned; the caller keeps [0]"


class TestPruneIsSafeByDefault:
    def test_dry_run_deletes_nothing(self, monkeypatch):
        """It removes monitoring coverage, so the harmless mode has to be default."""
        from src.eval import quality_alerts as qa

        deleted = []

        class _FakeClient:
            def list_alert_policies(self, name):
                return [
                    qa._build_policy("helpfulness", 3.0, [], family="agent_eval"),
                    qa._build_policy("helpfulness", 3.0, [], family="agent_eval"),
                ]

            def delete_alert_policy(self, name):
                deleted.append(name)

        monkeypatch.setattr(qa.monitoring_v3, "AlertPolicyServiceClient", _FakeClient)
        out = qa.prune_duplicate_alerts()
        assert deleted == []
        assert len(out) == 1

    def test_apply_deletes_all_but_one(self, monkeypatch):
        from src.eval import quality_alerts as qa

        deleted = []

        class _FakeClient:
            def list_alert_policies(self, name):
                pols = []
                for i in range(3):
                    p = qa._build_policy("helpfulness", 3.0, [], family="agent_eval")
                    p.name = f"projects/p/alertPolicies/{i}"
                    pols.append(p)
                return pols

            def delete_alert_policy(self, name):
                deleted.append(name)

        monkeypatch.setattr(qa.monitoring_v3, "AlertPolicyServiceClient", _FakeClient)
        qa.prune_duplicate_alerts(apply=True)
        assert len(deleted) == 2, "3 policies on one metric -> 2 removed, 1 kept"


class TestCreateIsIdempotent:
    """The actual regression: running setup twice must not double the fleet."""

    @staticmethod
    def _client(existing):
        from src.eval import quality_alerts as qa

        calls = {"created": [], "updated": []}

        class _FakeClient:
            def list_alert_policies(self, name):
                return list(existing)

            def create_alert_policy(self, name, alert_policy):
                calls["created"].append(alert_policy)
                alert_policy.name = "projects/p/alertPolicies/new"
                return alert_policy

            def update_alert_policy(self, alert_policy):
                calls["updated"].append(alert_policy)
                return alert_policy

        return qa, _FakeClient, calls

    def test_a_fresh_project_creates(self, monkeypatch):
        qa, FakeClient, calls = self._client([])
        monkeypatch.setattr(qa.monitoring_v3, "AlertPolicyServiceClient", FakeClient)
        qa.create_quality_alert("helpfulness", 3.0)
        assert len(calls["created"]) == 1
        assert calls["updated"] == []

    def test_running_it_again_updates_instead_of_duplicating(self, monkeypatch):
        from src.eval.quality_alerts import _build_policy

        prior = _build_policy("helpfulness", 3.0, [], family="agent_eval")
        prior.name = "projects/p/alertPolicies/existing"
        qa, FakeClient, calls = self._client([prior])
        monkeypatch.setattr(qa.monitoring_v3, "AlertPolicyServiceClient", FakeClient)
        qa.create_quality_alert("helpfulness", 3.0)
        assert calls["created"] == [], "THE bug: this used to create a second policy"
        assert len(calls["updated"]) == 1
        assert calls["updated"][0].name == prior.name

    def test_an_existing_online_policy_does_not_block_the_offline_one(self, monkeypatch):
        """Name collision would have made these look like the same policy."""
        from src.eval.quality_alerts import _build_policy

        online = _build_policy("helpfulness", 3.0, [], family="agent_online_eval")
        online.name = "projects/p/alertPolicies/online"
        qa, FakeClient, calls = self._client([online])
        monkeypatch.setattr(qa.monitoring_v3, "AlertPolicyServiceClient", FakeClient)
        qa.create_quality_alert("helpfulness", 3.0, family="agent_eval")
        assert len(calls["created"]) == 1
        assert calls["updated"] == []

    def test_a_changed_threshold_reaches_the_live_policy(self, monkeypatch):
        """Idempotent must mean convergent, not 'skip if present' — otherwise a
        threshold edit in code never reaches Cloud Monitoring."""
        from src.eval.quality_alerts import _build_policy

        prior = _build_policy("helpfulness", 3.0, [], family="agent_eval")
        prior.name = "projects/p/alertPolicies/existing"
        qa, FakeClient, calls = self._client([prior])
        monkeypatch.setattr(qa.monitoring_v3, "AlertPolicyServiceClient", FakeClient)
        qa.create_quality_alert("helpfulness", 4.2)
        assert calls["updated"][0].conditions[0].condition_threshold.threshold_value == 4.2
