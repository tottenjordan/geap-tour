"""Routing facts the paired experiments bought, pinned so they can't be lost.

`classifier_accuracy_pct` deliberately no longer answers "is the router sending this
band to the best tier?" — it grades the classifier on fixed reference bands, so it
stays put when boundaries are retuned (see `tests/test_reference_bands.py`).

That leaves the routing question needing a home. It belongs here rather than in a
monitored series: these are assertions about *config*, checkable offline from
`score_to_model_tier` with no GCP, no engine and no judge. Each one cost a paired
side-by-side to establish, so each docstring records the win-rate and p-value that
bought it — otherwise the next person has no way to weigh the constraint against
whatever they want to change.

Scores below are the values the classifier actually emits (0.10 / 0.40 / 0.75 /
0.85 / 0.90, zero overlap between bands), not hypotheticals.
"""

from __future__ import annotations

from src.router.complexity import score_to_model_tier, score_to_reference_band

# What the classifier emits per label, measured over the 40-case router eval set.
LOW_SCORE, MEDIUM_SCORE = 0.10, 0.40
HIGH_SCORES = (0.75, 0.85, 0.90)


class TestMediumBandMustNotRouteToLite:
    """flash beats lite on medium-complexity prompts: **18-1 (p=0.0001)** on the
    Gemini-3 tier pair and **14-2 (p=0.0042)** on the gemini-2.5 pair the router
    serves. `COMPLEXITY_LOW` was 0.44 and sent every 0.40 prompt to lite; it is now
    0.25. Regressing it re-buys a measured quality loss.
    """

    def test_a_medium_prompt_does_not_land_on_lite(self):
        assert score_to_model_tier(MEDIUM_SCORE) == "flash"

    def test_a_low_prompt_still_does(self):
        """The constraint is 'medium must not be lite', not 'nothing is lite' — lite
        is correct for the low band and was never tested against flash."""
        assert score_to_model_tier(LOW_SCORE) == "lite"


class TestHighBandTierSplit:
    """sonnet beats pro on high-complexity prompts: **12-2 (p=0.0129)** against
    `gemini-3.1-pro-preview` and **17-1 (p=0.0001)** against the `gemini-2.5-pro`
    the router serves.

    That evidence was originally POOLED over the whole band, while the router split
    the band at 0.80 — sending 0.75 to sonnet and 0.85/0.90 to pro, the tier that
    lost. `subband_split` settled it: the win holds on both sides, and the upper
    side (after 12 prompts were added to lift it from 6 decisive cases to 18) came
    back **17-1, p=0.0001**. `COMPLEXITY_HIGH` was raised 0.80 -> 0.925 as a result,
    so the whole measured band now routes to sonnet.
    """

    def test_no_measured_high_score_routes_to_pro(self):
        """The constraint the second experiment bought. Every score the classifier
        actually emits in this band must reach sonnet; a boundary regression that
        sends 0.85/0.90 back to pro reinstates a measured 17-1 quality loss."""
        assert all(score_to_model_tier(s) == "sonnet" for s in HIGH_SCORES)

    def test_pro_survives_as_a_narrow_window_above_the_measured_range(self):
        """Raising the cut did not delete the tier — it moved it above anything the
        classifier has been observed to emit. Real traffic scoring >= 0.925 still
        reaches pro, so this is a property of the eval set, not dead code."""
        assert score_to_model_tier(0.93) == "pro"
        assert max(HIGH_SCORES) < 0.925

    def test_neither_pro_nor_opus_receives_measured_traffic(self):
        """Stated plainly rather than left to be discovered: on this workload the
        5-tier router serves THREE tiers. opus was already unreachable
        (`HIGH_SPLIT`=0.95 vs a top observed score of 0.90); pro joined it when the
        cut rose. Nobody should claim five live tiers on this evidence."""
        assert all(score_to_model_tier(s) not in ("pro", "opus") for s in HIGH_SCORES)


def test_every_label_is_in_its_reference_band():
    """The classifier metric's premise: labels and emitted scores agree, so accuracy
    is 100% and any drop is a real classifier regression rather than boundary drift."""
    assert score_to_reference_band(LOW_SCORE) == "low"
    assert score_to_reference_band(MEDIUM_SCORE) == "medium"
    assert all(score_to_reference_band(s) == "high" for s in HIGH_SCORES)
