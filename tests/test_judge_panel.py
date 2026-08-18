"""Unit tests for the multi-model judge panel (:mod:`src.eval.judge_panel`)."""

from __future__ import annotations

import math

import pytest

from src.eval import judge_panel as jp


class TestMedianScore:
    def test_empty_is_none(self) -> None:
        assert jp.median_score([]) is None
        assert jp.median_score([None, None]) is None

    def test_ignores_none(self) -> None:
        assert jp.median_score([1.0, None, 3.0]) == pytest.approx(2.0)

    def test_odd_and_even(self) -> None:
        assert jp.median_score([0.2, 0.4, 0.9]) == pytest.approx(0.4)
        assert jp.median_score([0.2, 0.4, 0.6, 0.8]) == pytest.approx(0.5)


class TestScoreSpread:
    def test_fewer_than_two_valid_is_zero(self) -> None:
        assert jp.score_spread([]) == 0.0
        assert jp.score_spread([0.5]) == 0.0
        assert jp.score_spread([0.5, None]) == 0.0

    def test_max_minus_min(self) -> None:
        assert jp.score_spread([0.2, 0.9, 0.5]) == pytest.approx(0.7)


class TestMajorityLabel:
    def test_all_none_is_none_zero(self) -> None:
        label, agreement = jp.majority_label([None, None])
        assert label is None
        assert agreement == 0.0

    def test_picks_mode_and_agreement(self) -> None:
        label, agreement = jp.majority_label(["A", "A", "B"])
        assert label == "A"
        assert agreement == pytest.approx(2 / 3)

    def test_unanimous(self) -> None:
        label, agreement = jp.majority_label(["A", "A", "A"])
        assert label == "A"
        assert agreement == pytest.approx(1.0)


class TestKrippendorffAlpha:
    def test_too_few_pairable_is_nan(self) -> None:
        assert math.isnan(jp.krippendorff_alpha_interval([[1.0]]))
        assert math.isnan(jp.krippendorff_alpha_interval([[1.0, None]]))

    def test_perfect_agreement_is_one(self) -> None:
        alpha = jp.krippendorff_alpha_interval([[1.0, 1.0], [2.0, 2.0], [3.0, 3.0]])
        assert alpha == pytest.approx(1.0)

    def test_all_constant_is_one(self) -> None:
        # No variance anywhere -> perfect reliability by convention.
        assert jp.krippendorff_alpha_interval([[2.0, 2.0], [2.0, 2.0]]) == pytest.approx(1.0)

    def test_disagreement_below_agreement(self) -> None:
        agree = jp.krippendorff_alpha_interval([[1.0, 1.0], [5.0, 5.0]])
        disagree = jp.krippendorff_alpha_interval([[1.0, 5.0], [5.0, 1.0]])
        assert disagree < agree
        assert agree == pytest.approx(1.0)

    def test_missing_ratings_handled(self) -> None:
        # Units with <2 valid ratings are dropped, not crashed on.
        alpha = jp.krippendorff_alpha_interval([[1.0, 1.0], [None, 2.0], [3.0, 3.0]])
        assert alpha == pytest.approx(1.0)


class TestScoreWithPanel:
    def test_median_and_spread_across_judges(self) -> None:
        judges = [
            lambda _p: "Score: 4",
            lambda _p: "Score: 5",
            lambda _p: "Score: 3",
        ]
        result = jp.score_with_panel("q", judges, _parse_1_5)
        assert result["per_judge"] == [0.8, 1.0, 0.6]
        assert result["median"] == pytest.approx(0.8)
        assert result["spread"] == pytest.approx(0.4)
        assert result["n_valid"] == 3

    def test_unparseable_judge_dropped(self) -> None:
        judges = [
            lambda _p: "Score: 4",
            lambda _p: "no verdict",  # unparseable -> None -> dropped
            lambda _p: "Score: 2",
        ]
        result = jp.score_with_panel("q", judges, _parse_1_5)
        assert result["per_judge"] == [0.8, None, 0.4]
        assert result["median"] == pytest.approx(0.6)
        assert result["n_valid"] == 2


class TestPanelReliability:
    def test_aggregates_alpha_and_spread(self) -> None:
        per_item = [[1.0, 1.0], [0.5, 0.5], [0.2, 0.2]]
        rel = jp.panel_reliability(per_item)
        assert rel["alpha"] == pytest.approx(1.0)
        assert rel["mean_spread"] == pytest.approx(0.0)
        assert rel["n_items"] == 3
        assert rel["n_judges"] == 2

    def test_empty_is_safe(self) -> None:
        rel = jp.panel_reliability([])
        assert math.isnan(rel["alpha"])
        assert rel["mean_spread"] == 0.0
        assert rel["n_items"] == 0
        assert rel["n_judges"] == 0


class TestScorePairsWithPanel:
    def test_mean_of_medians_and_reliability(self) -> None:
        pairs = [("q1", "r1"), ("q2", "r2")]
        # Judge 3 is a contrarian on the second item -> median stays robust.
        judges = [
            lambda _p: "Score: 4",
            lambda _p: "Score: 4",
            lambda _p: "Score: 4",
        ]
        result = jp.score_pairs_with_panel(pairs, judges, _build_prompt, _parse_1_5)
        assert result["score"] == pytest.approx(0.8)
        assert result["n_scored"] == 2
        assert result["n_total"] == 2
        assert result["reliability"]["n_judges"] == 3
        assert result["reliability"]["alpha"] == pytest.approx(1.0)


class TestBuildPanel:
    def test_builds_one_fn_per_model_via_factory(self) -> None:
        seen = []

        def factory(model):
            seen.append(model)
            return lambda p: f"{model}:{p}"

        panel = jp.build_panel(models=("m1", "m2", "m3"), generate_fn_factory=factory)
        assert seen == ["m1", "m2", "m3"]
        assert len(panel) == 3
        assert panel[0]("x") == "m1:x"

    def test_default_models_are_diverse_generations(self) -> None:
        # A panel must span >1 model generation so a single version's blind spot
        # can't dominate the verdict. Gemini-only: the genai generateContent path
        # the judge client uses can't reach partner models like Claude (they 404
        # under publishers/google), so diversity is cross-generation, not cross-vendor.
        assert all(m.startswith("gemini") for m in jp.DEFAULT_PANEL_MODELS)
        generations = {m.split("-")[1].split(".")[0] for m in jp.DEFAULT_PANEL_MODELS}
        assert generations == {"2", "3"}  # spans Gemini-2 and Gemini-3
        assert len(jp.DEFAULT_PANEL_MODELS) >= 2


def _parse_1_5(text: str) -> float | None:
    import re

    m = re.findall(r"score\s*:?\s*([1-5])", str(text), re.IGNORECASE)
    return int(m[-1]) / 5.0 if m else None


def _build_prompt(prompt: str, response: str) -> str:
    return f"{prompt}||{response}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
