"""The classifier metric must not move when the ROUTER is retuned.

This file exists because it already happened. `routing_accuracy_pct` read 50% and
then 82.5% over the same 40 prompts with the same classifier, purely because
`COMPLEXITY_LOW` moved 0.44 -> 0.25. The metric was grading the classifier through
`_score_to_level`, which buckets scores with the *tunable routing cut-points*, so it
was scoring cut-point placement and reporting it as classifier skill. It also meant
every future boundary change — including one made *because* a paired experiment said
so — perturbed a monitored, alerting series.

`score_to_reference_band` fixes that by bucketing on constants that nothing may wire
to `COMPLEXITY_*`. These tests pin the decoupling itself, not just the arithmetic:
the invariance test below is the one that would have caught the original defect.

Pure: no GCP, no classifier call.
"""

from __future__ import annotations

import importlib

import pytest

from src.router.complexity import (
    LEVELS,
    REFERENCE_BANDS,
    score_to_model_tier,
    score_to_reference_band,
)


class TestReferenceBandsAreIndependentOfRouting:
    def test_the_bands_are_equal_thirds(self):
        """Equal thirds, not cluster midpoints: midpoints fitted to today's eval set
        would need re-deriving whenever the score distribution shifted, which is the
        same coupling in a new place."""
        assert REFERENCE_BANDS == (1 / 3, 2 / 3)

    def test_bands_do_not_move_when_the_routing_cut_points_do(self, monkeypatch):
        """THE test. Reload the module under the OLD boundaries and confirm every
        band assignment is unchanged — under the previous implementation the 0.40
        prompts would flip from medium to low."""
        before = {s: score_to_reference_band(s) for s in (0.10, 0.40, 0.75, 0.85, 0.90)}

        monkeypatch.setenv("COMPLEXITY_LOW", "0.44")
        monkeypatch.setenv("COMPLEXITY_HIGH", "0.60")
        import src.config
        import src.router.complexity as cx

        importlib.reload(src.config)
        reloaded = importlib.reload(cx)
        try:
            # Routing genuinely moved...
            assert reloaded.THRESHOLDS == [0.44, 0.60]
            assert reloaded.score_to_model_tier(0.40) == "lite"
            # ...but the classifier's grade did not.
            after = {s: reloaded.score_to_reference_band(s) for s in before}
            assert after == before
        finally:
            monkeypatch.undo()
            importlib.reload(src.config)
            importlib.reload(cx)

    def test_the_classifiers_real_scores_all_land_in_their_labelled_band(self):
        """The five scores the classifier actually emits, against their labels. This
        is why accuracy reads 100%: the classifier separates the bands with zero
        overlap, and the old 50%/82.5% was never measuring the classifier."""
        assert score_to_reference_band(0.10) == "low"
        assert score_to_reference_band(0.40) == "medium"
        for high in (0.75, 0.85, 0.90):
            assert score_to_reference_band(high) == "high"

    @pytest.mark.parametrize(
        ("score", "band"),
        [
            (0.0, "low"),
            (0.33, "low"),
            (1 / 3, "medium"),
            (0.5, "medium"),
            (2 / 3, "high"),
            (1.0, "high"),
        ],
    )
    def test_boundaries_are_inclusive_at_the_bottom_of_each_band(self, score, band):
        assert score_to_reference_band(score) == band

    def test_it_returns_only_known_levels(self):
        for i in range(101):
            assert score_to_reference_band(i / 100) in LEVELS


def test_band_and_tier_are_deliberately_different_questions():
    """A 0.75 prompt is in the top *band* but routes to the sonnet tier, not the top
    tier. That is not a bug — it is the separation this change introduces, and the
    paired experiment says sonnet is the right destination (17-1 over pro)."""
    assert score_to_reference_band(0.75) == "high"
    assert score_to_model_tier(0.75) == "sonnet"
