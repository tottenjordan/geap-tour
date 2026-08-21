"""Offline tests for the empty-rate concurrency sweep.

The sweep exists to answer one question with data: does the coordinator's residual
empty-at-200 rate track inference concurrency? Everything here is pure — the
engine-driving half is exercised live, not in CI.
"""

import pytest

from src.eval.stats import wilson_ci
from src.eval.sweep_empty_rate import arm_order, summarize, verdict


class TestArmOrder:
    """Running all repeats of one level together would confound the level with the
    time window it ran in."""

    def test_interleaves_levels_across_blocks(self):
        assert arm_order([1, 4, 8], 3) == [1, 4, 8, 4, 8, 1, 8, 1, 4]

    def test_every_level_appears_once_per_block(self):
        order = arm_order([1, 4, 8], 3)
        for block in range(3):
            assert sorted(order[block * 3 : (block + 1) * 3]) == [1, 4, 8]

    def test_single_repeat_is_just_the_levels(self):
        assert arm_order([1, 4], 1) == [1, 4]


class TestWilsonInterval:
    def test_zero_successes_has_a_nonzero_upper_bound(self):
        """The whole point of Wilson over normal-approx: 0/49 is not '0% +/- 0'."""
        low, high = wilson_ci(0, 49)
        assert low == 0.0
        assert 0.0 < high < 0.15

    def test_brackets_the_point_estimate(self):
        low, high = wilson_ci(7, 49)
        assert low < 7 / 49 < high

    def test_empty_total_is_safe(self):
        assert wilson_ci(0, 0) == (0.0, 0.0)


def _rows(pairs):
    """(workers, empty, n) -> sweep result rows."""
    return [
        {"workers": w, "empty": e, "n": n, "empty_indices": [], "exhausted": 0} for w, e, n in pairs
    ]


class TestSummarize:
    def test_pools_runs_per_arm_and_keeps_the_per_run_rates(self):
        s = summarize(_rows([(1, 0, 10), (1, 2, 10), (4, 5, 10)]))
        assert s[1]["runs"] == 2
        assert s[1]["empty"] == 2
        assert s[1]["total"] == 20
        assert s[1]["per_run"] == [0.0, 0.2]
        assert s[1]["spread"] == pytest.approx(0.2)
        assert s[4]["rate"] == pytest.approx(0.5)

    def test_spread_exposes_overdispersion(self):
        """Wide per-run spread at one level is the run-level clustering signal."""
        s = summarize(_rows([(4, 2, 49), (4, 13, 49)]))
        assert s[4]["spread"] > 0.2


class TestVerdict:
    """Criteria fixed before the numbers were seen."""

    def test_overlapping_intervals_report_no_detectable_effect(self):
        # 5/49 vs 7/49 — clearly indistinguishable at this n.
        out = verdict(summarize(_rows([(1, 5, 49), (8, 7, 49)])))
        assert "NO DETECTABLE EFFECT" in out

    def test_clean_separation_from_near_zero_is_contention(self):
        out = verdict(summarize(_rows([(1, 0, 147), (8, 45, 147)])))
        assert "SCALE-OUT CONTENTION" in out

    def test_high_floor_that_still_rises_is_reported_as_both(self):
        out = verdict(summarize(_rows([(1, 30, 147), (8, 80, 147)])))
        assert "CONCURRENCY-SENSITIVE" in out

    def test_flat_and_nonzero_is_a_steady_state_defect(self):
        """Equal rates cannot separate, so this must not claim contention."""
        out = verdict(summarize(_rows([(1, 60, 147), (8, 20, 147)])))
        assert "STEADY-STATE DEFECT" in out

    def test_one_level_is_inconclusive(self):
        assert "INCONCLUSIVE" in verdict(summarize(_rows([(4, 5, 49)])))
