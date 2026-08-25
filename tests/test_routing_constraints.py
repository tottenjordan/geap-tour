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

    That evidence is **pooled over the whole band**, and the router splits the band:
    0.75 goes to sonnet, 0.85/0.90 go to pro — the tier that lost. Whether the win
    holds on that upper sub-band is an OPEN QUESTION
    (`router_boundary_experiment.subband_split`). So these tests pin the current
    split as *observed behaviour* rather than asserting a constraint the evidence
    does not yet support. If the sub-band result comes in, tighten this.
    """

    def test_the_lower_high_band_routes_to_sonnet(self):
        assert score_to_model_tier(0.75) == "sonnet"

    def test_the_upper_high_band_still_routes_to_pro(self):
        """Documented, not endorsed. The pooled result says sonnet > pro on this
        band; if that holds at 0.85/0.90 these 5-ish cases are misrouted and
        COMPLEXITY_HIGH should rise."""
        assert score_to_model_tier(0.85) == "pro"
        assert score_to_model_tier(0.90) == "pro"

    def test_no_emitted_score_reaches_opus(self):
        """`HIGH_SPLIT` is 0.95 and the top score observed is 0.90, so the opus tier
        receives nothing on the current eval set — a deployed, always-warm engine the
        router cannot reach. Real traffic could still exceed 0.95; this pins only
        what is measured."""
        assert all(score_to_model_tier(s) != "opus" for s in HIGH_SCORES)


def test_every_label_is_in_its_reference_band():
    """The classifier metric's premise: labels and emitted scores agree, so accuracy
    is 100% and any drop is a real classifier regression rather than boundary drift."""
    assert score_to_reference_band(LOW_SCORE) == "low"
    assert score_to_reference_band(MEDIUM_SCORE) == "medium"
    assert all(score_to_reference_band(s) == "high" for s in HIGH_SCORES)
