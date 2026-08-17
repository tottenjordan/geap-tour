"""Unit tests for the pure statistical-rigor helpers (:mod:`src.eval.stats`)."""

from __future__ import annotations

import math

import pytest

from src.eval import stats


class TestSampleFloor:
    def test_below_floor_is_low_confidence(self) -> None:
        assert stats.is_low_confidence(stats.MIN_SAMPLES - 1)
        assert stats.confidence_label(stats.MIN_SAMPLES - 1) == "low_confidence"

    def test_at_or_above_floor_is_ok(self) -> None:
        assert not stats.is_low_confidence(stats.MIN_SAMPLES)
        assert not stats.is_low_confidence(stats.MIN_SAMPLES + 5)
        assert stats.confidence_label(stats.MIN_SAMPLES) == "ok"

    def test_custom_floor(self) -> None:
        assert stats.is_low_confidence(3, floor=5)
        assert not stats.is_low_confidence(5, floor=5)


class TestBootstrapMeanCI:
    def test_empty_is_nan(self) -> None:
        lo, hi = stats.bootstrap_mean_ci([])
        assert math.isnan(lo) and math.isnan(hi)

    def test_single_value_is_degenerate(self) -> None:
        assert stats.bootstrap_mean_ci([3.0]) == (3.0, 3.0)

    def test_constant_values_have_zero_width(self) -> None:
        lo, hi = stats.bootstrap_mean_ci([2.0, 2.0, 2.0, 2.0, 2.0])
        assert lo == pytest.approx(2.0)
        assert hi == pytest.approx(2.0)

    def test_ci_brackets_the_mean(self) -> None:
        vals = [1.0, 2.0, 3.0, 4.0, 5.0, 2.0, 3.0, 4.0]
        lo, hi = stats.bootstrap_mean_ci(vals, seed=7)
        assert lo <= sum(vals) / len(vals) <= hi

    def test_deterministic_with_seed(self) -> None:
        vals = [1.0, 5.0, 2.0, 4.0, 3.0, 1.0, 5.0]
        assert stats.bootstrap_mean_ci(vals, seed=1) == stats.bootstrap_mean_ci(vals, seed=1)

    def test_wider_spread_gives_wider_interval(self) -> None:
        tight = stats.bootstrap_mean_ci([3.0, 3.1, 2.9, 3.0, 3.1, 2.9], seed=3)
        wide = stats.bootstrap_mean_ci([0.0, 6.0, 1.0, 5.0, 0.0, 6.0], seed=3)
        assert (wide[1] - wide[0]) > (tight[1] - tight[0])


class TestBinomialTwoSidedP:
    def test_empty_is_one(self) -> None:
        assert stats.binomial_two_sided_p(0, 0) == 1.0

    def test_symmetric_at_half_is_one(self) -> None:
        assert stats.binomial_two_sided_p(5, 10) == pytest.approx(1.0)

    def test_extreme_outcome_is_small(self) -> None:
        p = stats.binomial_two_sided_p(10, 10)
        assert p < 0.01

    def test_is_a_probability(self) -> None:
        for k in range(21):
            p = stats.binomial_two_sided_p(k, 20)
            assert 0.0 <= p <= 1.0


class TestWilsonCI:
    def test_empty_is_zero_width_at_zero(self) -> None:
        assert stats.wilson_ci(0, 0) == (0.0, 0.0)

    def test_bounds_stay_in_unit_interval(self) -> None:
        lo, hi = stats.wilson_ci(10, 10)
        assert 0.0 <= lo <= hi <= 1.0

    def test_all_wins_lower_bound_below_one(self) -> None:
        lo, hi = stats.wilson_ci(10, 10)
        assert lo < 1.0
        assert hi == pytest.approx(1.0, abs=1e-9) or hi <= 1.0

    def test_center_tracks_proportion(self) -> None:
        lo, hi = stats.wilson_ci(5, 10)
        assert lo < 0.5 < hi


class TestWinRateSignificance:
    def test_decisive_sweep_is_significant(self) -> None:
        r = stats.win_rate_significance(10, 0)
        assert r["significant"] is True
        assert r["p_value"] < 0.05
        assert r["decisive"] == 10
        assert r["win_rate_decisive"] == pytest.approx(1.0)

    def test_even_split_is_not_significant(self) -> None:
        r = stats.win_rate_significance(5, 5)
        assert r["significant"] is False
        assert r["p_value"] == pytest.approx(1.0)

    def test_no_decisive_cases_is_not_significant(self) -> None:
        r = stats.win_rate_significance(0, 0)
        assert r["significant"] is False
        assert r["decisive"] == 0
        assert r["p_value"] == 1.0

    def test_ties_excluded_from_denominator(self) -> None:
        # ties are not passed in; denominator is wins+losses only
        r = stats.win_rate_significance(8, 2)
        assert r["decisive"] == 10
        assert r["win_rate_decisive"] == pytest.approx(0.8)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
