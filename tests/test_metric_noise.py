"""A floor is a claim about how far a metric wanders.

`agent_router_quality/*`'s thresholds were set from observed *levels* — where each
metric sits — which says nothing about how much it moves when nothing changed. So
an alert could have been either permanently silent or permanently flapping and both
would have looked identical at setup time.

`src/eval/spike_metric_noise.py` measured it on 2026-09-17: one inference capture
scored three times (judge variance), against three full runs over the same 20 cases
and the same engine (total variance). Variances add, so the difference is the agent.

These tests cover the arithmetic that decomposition depends on, and keep the
measured numbers next to the thresholds they justify.
"""

from __future__ import annotations

import pathlib

import pytest

from src.eval.spike_metric_noise import (
    agent_sd,
    detectable_shift,
    scale_to_five,
    summarize,
)


class TestTheVarianceDecomposition:
    def test_variances_subtract_not_standard_deviations(self):
        """THE arithmetic. Subtracting sds directly (0.335 - 0.086 = 0.249) is the
        intuitive move and it is wrong; variances add, so the agent's share is
        sqrt(0.335^2 - 0.086^2) = 0.324. The difference decides whether
        instruction_following looks 74% or 93% agent-driven."""
        got = agent_sd(total_sd=0.335, judge_sd=0.086)
        assert got == pytest.approx(0.324, abs=0.001)
        assert got != pytest.approx(0.335 - 0.086, abs=0.001)

    def test_a_judge_noisier_than_the_total_clamps_to_zero(self):
        """Sampling noise over three runs can put the judge sd above the measured
        total. That is not a negative variance, and returning nan would poison a
        report that is otherwise fine."""
        assert agent_sd(total_sd=0.10, judge_sd=0.30) == 0.0

    def test_no_judge_noise_attributes_everything_to_the_agent(self):
        assert agent_sd(total_sd=0.4, judge_sd=0.0) == pytest.approx(0.4)

    def test_the_detection_limit_is_two_sigma(self):
        assert detectable_shift(0.335) == pytest.approx(0.67, abs=0.005)
        assert detectable_shift(0.049) == pytest.approx(0.10, abs=0.005)


class TestTheScaleMatchesThePublishedAxis:
    @pytest.mark.parametrize(("raw", "want"), [(0.0, 1.0), (0.5, 3.0), (1.0, 5.0)])
    def test_zero_to_one_maps_onto_one_to_five(self, raw, want):
        """Must match publish_router_quality's transform exactly, or the noise
        figures describe a different axis than the floors they are compared to."""
        assert scale_to_five(raw) == pytest.approx(want)

    def test_it_agrees_with_the_publisher(self):
        from src.eval.publish_router_quality import extract_router_quality

        published = extract_router_quality({"metrics": {"runtime_0/safety_v1": {"score": 0.82}}})[
            "safety"
        ]
        assert published == pytest.approx(scale_to_five(0.82), abs=0.01)


class TestSummaryStats:
    def test_it_reports_spread_not_just_the_mean(self):
        """A mean alone is what hid this problem: 3.20 and 3.79 average to a
        perfectly reasonable 3.50."""
        s = summarize([3.20, 3.79, 3.22])
        assert s["n"] == 3
        assert s["mean"] == pytest.approx(3.403, abs=0.001)
        assert s["sd"] == pytest.approx(0.335, abs=0.001)
        assert s["range"] == pytest.approx(0.59, abs=0.001)

    def test_a_single_sample_has_no_spread_rather_than_crashing(self):
        """n=1 is the normal state of a young series — statistics.stdev raises."""
        assert summarize([4.0]) == {"n": 1, "mean": 4.0, "sd": 0.0, "range": 0.0}


class TestTheMeasurementStaysNextToTheThresholds:
    """Recorded where someone tuning a floor will actually encounter it.

    The same reason the aiplatform pin carries its reason inline: a number with no
    recorded basis gets changed by the next person who finds it inconvenient.
    """

    ALERTS = pathlib.Path(__file__).resolve().parents[1] / "src/eval/quality_alerts.py"

    def test_the_noise_figures_are_recorded(self):
        text = self.ALERTS.read_text()
        assert "spike_metric_noise" in text, "the measurement must name its source"
        for figure in ("0.335", "0.275", "0.086"):
            assert figure in text, f"measured sd {figure} is no longer recorded"

    def test_the_agent_vs_judge_split_is_stated(self):
        """The two metrics need opposite fixes; a reader who misses that will reach
        for a judge panel to calm an agent-driven series."""
        text = self.ALERTS.read_text()
        assert "AGENT-dominated" in text
        assert "JUDGE-dominated" in text

    def test_the_near_inert_alerts_are_labelled(self):
        """hallucination and safety sit 21-27 sd above their floors. Keeping them is
        fine; mistaking their green for protection is not."""
        text = self.ALERTS.read_text()
        assert "inert" in text
