"""The router's two blind spots, and the guards that close them.

Before this, every `agent_router/*` series measured the router's **decision** and
none measured its **answer**:

* a routing collapse toward the cheap tier moved nothing — `classifier_accuracy_pct`
  grades the classifier's raw score against *fixed reference bands*, deliberately not
  the tunable cut-points, so a boundary change leaves it untouched; and
  `cost_savings_pct` *rises*, 93.1% -> ~99.6%. Both monitored numbers look BETTER
  while the router has stopped routing;
* nothing scored the router's responses at all, despite 40 eval cases and six
  rubrics already wired up.

These tests defend the two additions against the specific ways each would go quiet.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from src.eval import publish_router_efficiency as pre
from src.eval import publish_router_quality as prq


class _CaptureWriter:
    """Records gauge writes instead of calling Cloud Monitoring."""

    def __init__(self, sink: dict) -> None:
        self.sink = sink

    def write_gauge(self, name, value, labels):
        self.sink[name] = value


def _cost(tiers: list[str]) -> dict:
    return {"per_case": [{"tier": t} for t in tiers], "savings_pct": 93.1}


class TestTierSpreadSeesACollapse:
    """`lite_tier_pct` + `tiers_used` are the only things that can."""

    def test_the_healthy_distribution(self):
        """Measured 2026-09-17: lite=14 flash=13 sonnet=13 of 40."""
        spread = pre.tier_spread(_cost(["lite"] * 14 + ["flash"] * 13 + ["sonnet"] * 13))
        assert spread == {"lite_tier_pct": 35.0, "tiers_used": 3.0}

    def test_a_total_collapse_to_lite_is_visible(self):
        """THE case. Savings would go UP here; these two are what fire."""
        spread = pre.tier_spread(_cost(["lite"] * 40))
        assert spread["lite_tier_pct"] == 100.0
        assert spread["tiers_used"] == 1.0

    def test_a_collapse_onto_an_expensive_tier_is_also_visible(self):
        """`lite_tier_pct` alone would read 0.0 and look healthy; `tiers_used` is
        why both metrics exist."""
        spread = pre.tier_spread(_cost(["opus"] * 40))
        assert spread["lite_tier_pct"] == 0.0
        assert spread["tiers_used"] == 1.0

    def test_both_cross_their_alert_thresholds_on_a_collapse(self):
        """A metric nobody alerts on is a chart. Check the real thresholds."""
        from src.eval.quality_alerts import ROUTER_MONITORED_METRICS

        limits = {n: (t, c) for n, t, c in ROUTER_MONITORED_METRICS}
        spread = pre.tier_spread(_cost(["lite"] * 40))

        lite_t, lite_cmp = limits["lite_tier_pct"]
        used_t, used_cmp = limits["tiers_used"]
        assert lite_cmp == "GT" and spread["lite_tier_pct"] > lite_t
        assert used_cmp == "LT" and spread["tiers_used"] < used_t

    def test_the_healthy_distribution_does_not_alert(self):
        """The other half of a threshold: it must not fire on normal traffic."""
        from src.eval.quality_alerts import ROUTER_MONITORED_METRICS

        limits = {n: t for n, t, _c in ROUTER_MONITORED_METRICS}
        spread = pre.tier_spread(_cost(["lite"] * 14 + ["flash"] * 13 + ["sonnet"] * 13))
        assert spread["lite_tier_pct"] < limits["lite_tier_pct"]
        assert spread["tiers_used"] >= limits["tiers_used"]

    def test_no_per_case_rows_publishes_nothing(self):
        """Never a misleading 0 — a partial run must omit, not invent."""
        assert pre.tier_spread({}) == {}
        assert pre.tier_spread({"per_case": []}) == {}

    def test_the_published_payload_carries_them(self):
        written: dict = {}
        pre.publish_router_efficiency(
            {"accuracy": 1.0, "avg_latency_ms": 657.8},
            _cost(["lite"] * 14 + ["flash"] * 13 + ["sonnet"] * 13),
            writer=_CaptureWriter(written),
            log_run_fn=lambda **_k: False,
        )
        assert written["agent_router/lite_tier_pct"] == 35.0
        assert written["agent_router/tiers_used"] == 3.0

    def test_the_log_and_the_metric_cannot_disagree(self):
        """Both read `tier_counts`; two counters would drift."""
        cost = _cost(["lite", "lite", "flash"])
        assert pre.tier_counts(cost) == {"lite": 2, "flash": 1}
        assert "lite=2" in pre.format_distribution({}, cost)


class TestRouterQualityPublishesTheBadNews:
    """A quality series that drops its low points only ever shows good news."""

    # The REAL shape `_run_single_agent_eval` stores: a detail dict per metric, not
    # a float. A first version of these tests used floats, passed, and the live run
    # raised `TypeError: float() argument must be ... not 'dict'`.
    REAL: ClassVar[dict] = {
        "status": "FAILED",  # means "below threshold" here, NOT "the run broke"
        "metrics": {
            "runtime_0/final_response_quality_v1": {"score": 0.71, "passed": True},
            "runtime_0/hallucination_v1": {"score": 0.90, "passed": True},
            "runtime_0/safety_v1": {"score": 0.82, "passed": True},
            "runtime_0/instruction_following_v1": {"score": 0.55, "passed": False},
            "runtime_0/tool_use_quality_v1": {"score": 0.57, "passed": False},
        },
    }

    def test_a_below_threshold_run_still_publishes(self):
        """THE property. `status` is overloaded — `"FAILED"` means both "the eval run
        broke" and "the scores were low". Keying on it suppressed the second case,
        which is the one the alerts exist for."""
        scores = prq.extract_router_quality(self.REAL)
        assert scores["instruction_following"] == 3.2
        assert len(scores) == 4

    def test_a_broken_run_publishes_nothing(self):
        assert prq.extract_router_quality({"status": "FAILED", "error": "boom"}) == {}

    def test_an_all_empty_run_publishes_nothing(self):
        """Infra, not quality — a 0 here would render as catastrophic quality."""
        assert prq.extract_router_quality({"status": "SKIPPED", "metrics": {}}) == {}

    def test_scores_are_scaled_to_the_one_to_five_axis(self):
        """Same transform as publish_offline_eval, so the numbers are comparable."""
        assert prq.extract_router_quality(self.REAL)["safety"] == round(1 + 0.82 * 4, 2)

    def test_tool_use_is_deliberately_not_published(self):
        """The generic TOOL_USE_QUALITY rubric is a confirmed false-negative for a
        domain router. Publishing a known-suspect number into an alerting series is
        how a green tick stops meaning anything."""
        assert "tool_use" not in prq.extract_router_quality(self.REAL)
        assert "tool_use_quality" not in prq.extract_router_quality(self.REAL)

    def test_the_detail_dict_shape_is_handled(self):
        assert prq._score_of({"score": 0.5, "passed": True}) == 0.5
        assert prq._score_of(0.5) == 0.5
        assert prq._score_of({"passed": True}) is None
        assert prq._score_of("not a number") is None

    def test_the_unstable_candidate_prefix_is_stripped(self):
        """`runtime_0/` on aiplatform 2.x, `agent_engine_0/` on 1.x."""
        for prefix in ("runtime_0", "agent_engine_0"):
            got = prq.extract_router_quality({"metrics": {f"{prefix}/safety_v1": {"score": 1.0}}})
            assert got == {"safety": 5.0}, prefix

    def test_it_lands_on_its_own_family_not_agent_router(self):
        """`agent_router/*` is percents and milliseconds; these are 1-5 scores.
        One family holding both axes is how a dashboard averages a latency into a
        score."""
        written: dict = {}
        prq.publish_router_quality(
            self.REAL,
            writer=_CaptureWriter(written),
        )
        assert all(n.startswith("agent_router_quality/") for n in written)
        assert written["agent_router_quality/instruction_following"] == 3.2


class TestTheThresholdsAreSupportedByMeasurement:
    def test_instruction_following_floor_sits_below_its_observed_range(self):
        """Measured 2.72 (n=6) and 3.20 (n=20). A 3.0 floor is INSIDE that range, so
        it would flap on sampling variation — and a flapping alert gets muted."""
        from src.eval.quality_alerts import ROUTER_QUALITY_MONITORED_METRICS

        floors = dict(ROUTER_QUALITY_MONITORED_METRICS)
        assert floors["instruction_following"] < 2.72

    @pytest.mark.parametrize(
        ("metric", "observed_min"),
        [("response_quality", 3.84), ("hallucination", 4.60), ("safety", 4.28)],
    )
    def test_the_other_floors_have_headroom(self, metric, observed_min):
        from src.eval.quality_alerts import ROUTER_QUALITY_MONITORED_METRICS

        assert dict(ROUTER_QUALITY_MONITORED_METRICS)[metric] < observed_min

    def test_every_published_name_is_monitored(self):
        """A series nobody alerts on is a chart. Names must match exactly."""
        from src.eval.quality_alerts import ROUTER_QUALITY_MONITORED_METRICS

        monitored = {n for n, _t in ROUTER_QUALITY_MONITORED_METRICS}
        assert set(prq.METRIC_ALIASES.values()) <= monitored

    def test_the_metric_descriptors_follow_the_alert_source(self):
        from src.observability.metrics import ROUTER_QUALITY_METRIC_TYPES

        assert any("instruction_following" in t for t in ROUTER_QUALITY_METRIC_TYPES)
        assert all("agent_router_quality/" in t for t in ROUTER_QUALITY_METRIC_TYPES)
