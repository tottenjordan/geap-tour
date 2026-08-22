"""Unit tests for the judge-vs-human calibration framework (:mod:`src.eval.calibration`)."""

from __future__ import annotations

import math

import pytest

from src.eval import calibration as cal


class TestPearson:
    def test_perfect_positive(self) -> None:
        assert cal.pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)

    def test_perfect_negative(self) -> None:
        assert cal.pearson([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == pytest.approx(-1.0)

    def test_zero_variance_is_nan(self) -> None:
        assert math.isnan(cal.pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]))

    def test_too_few_is_nan(self) -> None:
        assert math.isnan(cal.pearson([1.0], [2.0]))


class TestCalibrationMetrics:
    def test_perfect_agreement(self) -> None:
        m = cal.calibration_metrics([0.8, 0.6, 1.0], [0.8, 0.6, 1.0])
        assert m["n"] == 3
        assert m["mae"] == pytest.approx(0.0)
        assert m["bias"] == pytest.approx(0.0)
        assert m["within_tolerance"] == pytest.approx(1.0)
        assert m["pearson"] == pytest.approx(1.0)
        assert m["n_unparseable"] == 0

    def test_lenient_judge_has_positive_bias(self) -> None:
        # judge scores consistently higher than the human -> positive bias.
        m = cal.calibration_metrics([0.4, 0.6, 0.2], [0.8, 1.0, 0.6])
        assert m["bias"] > 0
        assert m["mae"] == pytest.approx(0.4)

    def test_unparseable_dropped_but_counted(self) -> None:
        m = cal.calibration_metrics([0.8, 0.6, 1.0], [0.8, None, 1.0])
        assert m["n"] == 2  # the None pair is dropped from the stats
        assert m["n_unparseable"] == 1
        assert m["mae"] == pytest.approx(0.0)

    def test_tolerance_boundary(self) -> None:
        # diff of exactly the tolerance counts as within.
        m = cal.calibration_metrics([0.5, 0.5], [0.7, 0.9], tolerance=0.2)
        assert m["within_tolerance"] == pytest.approx(0.5)

    def test_empty_is_safe(self) -> None:
        m = cal.calibration_metrics([], [])
        assert m["n"] == 0
        assert math.isnan(m["mae"])
        assert math.isnan(m["pearson"])


class TestScoreJudgeVsGold:
    def test_scores_gold_and_reports_per_case(self) -> None:
        gold = [
            {"prompt": "p1", "response": "r1", "human_score": 5, "metric": "policy_compliance"},
            {"prompt": "p2", "response": "r2", "human_score": 3, "metric": "policy_compliance"},
        ]
        # Judge says 5/5 then 3/5 -> normalized 1.0, 0.6 == human -> perfect.
        replies = iter(["Score: 5", "Score: 3"])
        result = cal.score_judge_vs_gold(
            gold,
            lambda _p: next(replies),
            _build_prompt,
            _parse_1_5,
        )
        assert result["n"] == 2
        assert result["mae"] == pytest.approx(0.0)
        assert result["within_tolerance"] == pytest.approx(1.0)
        assert len(result["per_case"]) == 2
        assert result["per_case"][0]["human"] == pytest.approx(1.0)
        assert result["per_case"][0]["judge"] == pytest.approx(1.0)

    def test_disagreement_lowers_within_tolerance(self) -> None:
        gold = [{"prompt": "p", "response": "r", "human_score": 5, "metric": "policy_compliance"}]
        result = cal.score_judge_vs_gold(gold, lambda _p: "Score: 1", _build_prompt, _parse_1_5)
        assert result["within_tolerance"] == pytest.approx(0.0)
        assert result["mae"] == pytest.approx(0.8)


class TestScorePanelVsGold:
    def test_panel_median_vs_gold(self) -> None:
        gold = [{"prompt": "p", "response": "r", "human_score": 4, "metric": "policy_compliance"}]
        judges = [lambda _p: "Score: 4", lambda _p: "Score: 4", lambda _p: "Score: 2"]
        result = cal.score_panel_vs_gold(gold, judges, _build_prompt, _parse_1_5)
        # panel median is 4/5 == human 4/5 -> perfect agreement, robust to outlier.
        assert result["mae"] == pytest.approx(0.0)
        assert result["reliability"]["n_judges"] == 3


class TestGoldSet:
    def test_ships_a_curated_policy_gold_set(self) -> None:
        gold = cal.load_gold_set()
        assert len(gold) >= 25
        for case in gold:
            assert case["prompt"]
            assert case["response"]
            assert case["metric"] == "policy_compliance"
            assert case["difficulty"] in {"contrast", "hard"}
            # `annotations` is the source of truth; a case may legitimately be
            # unscored while it waits for a blind annotation pass.
            for score in (case.get("annotations") or {}).values():
                assert 1 <= score <= 5

    def test_gold_spans_low_and_high_scores(self) -> None:
        # A calibration set that is all-good or all-bad can't detect bias.
        scores = {cal.consensus_score(c) for c in cal.scored_cases(cal.load_gold_set())}
        assert min(scores) <= 2
        assert max(scores) >= 4

    def test_gold_carries_hard_cases_for_sensitivity(self) -> None:
        """v2 was effectively binary — 16 fives, 15 at or below 2, ONE midscale — so
        a judge separated it trivially and agreement pinned at 100%, a gate that
        could only move down. The hard band is what restores sensitivity."""
        gold = cal.load_gold_set()
        hard = [c for c in gold if c["difficulty"] == "hard"]
        assert len(hard) >= 15

    def test_verbose_and_terse_forms_of_one_answer_are_both_present(self) -> None:
        """A deliberate probe: the same correct verdict stated tersely and at
        length must score alike. `geap_tool_use` was caught grading exactly this
        difference as if it were quality."""
        prompts = [c["prompt"] for c in cal.load_gold_set() if c["difficulty"] == "hard"]
        assert any(prompts.count(p) >= 2 for p in set(prompts))


def _parse_1_5(text: str) -> float | None:
    import re

    m = re.findall(r"score\s*:?\s*([1-5])", str(text), re.IGNORECASE)
    return int(m[-1]) / 5.0 if m else None


def _build_prompt(prompt: str, response: str) -> str:
    return f"{prompt}||{response}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
