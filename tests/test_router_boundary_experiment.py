"""The boundary experiment must be trustworthy *before* it is run, not after.

Three properties carry the whole result, and each has a way of failing silently:

1. **Case selection.** The two sources have different shapes — filtering both on
   ``expected_complexity`` drops all 7 tier cases and nothing complains, it just
   quietly costs a third of the sample.
2. **Comparison orientation.** ``win_rate_candidate > 0.5`` means "the bigger model
   wins". Swap baseline and candidate and every verdict inverts while still
   reading as a plausible experiment.
3. **The decision rule.** It is pre-registered precisely so it cannot be adjusted
   once the numbers are in; these tests pin it to the published table.

No GCP, no judge: everything is exercised through the injected ``run_fn``.
"""

from __future__ import annotations

import dataclasses

import pytest

from src.eval.pairwise_eval import BASELINE, CANDIDATE, TIE, aggregate_choices
from src.eval.router_boundary_experiment import (
    COMPARISONS,
    EQUIVALENCE_BOUND,
    Comparison,
    format_report,
    main,
    run_comparison,
    select_cases,
    verdict,
)


def _result(choices: list[str]) -> dict:
    """A pairwise result built the way the real one is — via aggregate_choices."""
    return aggregate_choices(choices)


class TestCaseSelection:
    def test_both_sources_contribute(self):
        """The defect this guards: TIER_EVAL_CASES items have NO expected_complexity
        field (the band is the dict key), so a naive filter returns router cases only."""
        from src.eval.tier_eval_cases import TIER_EVAL_CASES

        selected = {c["prompt"] for c in select_cases("medium")}
        tier_prompts = {c["prompt"] for c in TIER_EVAL_CASES["medium"]}
        assert tier_prompts <= selected, "tier cases were dropped by the band filter"

    @pytest.mark.parametrize(("band", "expected"), [("medium", 19), ("high", 32)])
    def test_verified_counts(self, band, expected):
        """Pinned because the power analysis is stated in terms of these numbers.

        medium n=19 needs 15 decisive wins for p<0.05; high n=32 needs 23. The high
        band grew 20 -> 32 to settle COMPLEXITY_HIGH: the upper sub-band (the one
        the router sends to `pro`) had only 6 decisive cases, where 6-0 gives
        p=0.0312 and a single loss gives nothing. Twelve added prompts, all verified
        to score >= 0.80, take that sub-band from 8 prompts to 20."""
        assert len(select_cases(band)) == expected

    def test_prompts_are_deduped(self):
        """The two sources share one medium prompt; judging it twice would inflate n."""
        for band in ("medium", "high"):
            prompts = [c["prompt"] for c in select_cases(band)]
            assert len(prompts) == len(set(prompts)), band

    def test_only_the_requested_band_is_returned(self):
        for c in select_cases("high"):
            assert c.get("expected_complexity") in (None, "high"), c["prompt"][:40]

    def test_an_unknown_band_is_empty_not_an_error(self):
        assert select_cases("nonexistent") == []


class TestComparisonOrientation:
    def test_baseline_is_always_the_cheaper_current_tier(self):
        """A silent inversion here flips every verdict while still looking sane.
        The tier order is lite < flash < pro, and sonnet is the tier a 0.75 prompt
        hits today under COMPLEXITY_HIGH=0.80."""
        assert [(c.band, c.baseline_agent, c.candidate_agent) for c in COMPARISONS] == [
            ("medium", "lite_agent", "flash_agent"),
            ("high", "sonnet_agent", "pro_agent"),
        ]

    def test_every_comparison_names_the_boundary_it_tests(self):
        """A result nobody can trace back to a cut-point cannot drive a change."""
        for c in COMPARISONS:
            assert "COMPLEXITY_" in c.boundary and "=" in c.boundary
            assert len(c.note) > 30

    def test_the_boundary_label_reads_live_config_not_a_literal(self):
        """The first version baked the value into the string, so every report kept
        printing `COMPLEXITY_LOW=0.44` after the cut-point moved to 0.25 — a stale
        number presented as the setting under test."""
        from src import config

        assert COMPARISONS[0].boundary == f"COMPLEXITY_LOW={config.COMPLEXITY_LOW}"
        assert COMPARISONS[1].boundary == f"COMPLEXITY_HIGH={config.COMPLEXITY_HIGH}"

    def test_miscut_active_tracks_whether_the_cut_still_sits_above_the_band(self):
        """`baseline_agent` is "the tier this band gets now", which stops being true
        once the cut moves below the band's score. At the shipped COMPLEXITY_LOW=0.25
        the medium miscut is FIXED; at 0.80 the high one is still live."""
        assert COMPARISONS[0].band_score == 0.40
        assert COMPARISONS[0].miscut_active is False  # 0.25 < 0.40 -> already routed to flash
        assert COMPARISONS[1].band_score == 0.75
        assert COMPARISONS[1].miscut_active is True  # 0.80 > 0.75 -> still lands on sonnet

    def test_a_reverted_boundary_makes_the_medium_miscut_live_again(self, monkeypatch):
        monkeypatch.setattr("src.config.COMPLEXITY_LOW", 0.44)
        assert COMPARISONS[0].miscut_active is True
        assert COMPARISONS[0].boundary == "COMPLEXITY_LOW=0.44"


class TestPreRegisteredDecisionRule:
    def test_a_clear_candidate_win_says_retune(self):
        v = verdict(_result([CANDIDATE] * 16 + [BASELINE] * 3))
        assert v["verdict"] == "CANDIDATE_BETTER"
        assert "retune" in v["reading"]

    def test_a_clear_baseline_win_says_the_labels_are_wrong(self):
        """The cheap tier winning is a real possible outcome and it does NOT mean
        'keep the boundary' — it means the complexity labels are miscalibrated."""
        v = verdict(_result([BASELINE] * 16 + [CANDIDATE] * 3))
        assert v["verdict"] == "BASELINE_BETTER"
        assert "labels are wrong" in v["reading"]

    def test_an_even_split_over_few_cases_is_inconclusive_not_a_null(self):
        """THE test that matters. 3-3 is a 50% win rate, but the interval still
        admits a large effect; calling that 'no difference' would be the exact
        underpowered-verdict error the calibration gate exists to prevent."""
        v = verdict(_result([CANDIDATE] * 3 + [BASELINE] * 3))
        assert v["verdict"] == "INCONCLUSIVE"
        assert v["needed_decisive_n"] and v["needed_decisive_n"] > 6

    def test_an_even_split_over_many_cases_establishes_equivalence(self):
        v = verdict(_result([CANDIDATE] * 30 + [BASELINE] * 30))
        assert v["verdict"] == "NO_DIFFERENCE"
        assert "not what decides answer quality" in v["reading"]

    def test_all_ties_is_inconclusive(self):
        """Zero decisive cases must not read as a 0% win rate for the baseline."""
        v = verdict(_result([TIE] * 20))
        assert v["verdict"] == "INCONCLUSIVE"

    def test_equivalence_needs_the_interval_to_exclude_an_effect_both_ways(self):
        """A lopsided-but-not-significant result is underpowered, not equivalent."""
        v = verdict(_result([CANDIDATE] * 6 + [BASELINE] * 2))
        assert v["verdict"] == "INCONCLUSIVE"

    def test_the_bound_is_symmetric(self):
        """0.75 for the candidate and 0.25 for the baseline must be treated alike,
        or the rule quietly favours one side of the trade-off."""
        assert 0.5 < EQUIVALENCE_BOUND < 1.0
        low = verdict(_result([CANDIDATE] * 30 + [BASELINE] * 30))
        assert low["verdict"] == "NO_DIFFERENCE"

    def test_the_actual_power_claim_holds(self):
        """The plan promises 15/19 and 15/20 reach p < 0.05 and one fewer does not.
        That claim is why the experiment uses the whole band, so pin it."""
        assert _result([CANDIDATE] * 15 + [BASELINE] * 4)["significance"]["significant"]
        assert not _result([CANDIDATE] * 14 + [BASELINE] * 5)["significance"]["significant"]
        assert _result([CANDIDATE] * 15 + [BASELINE] * 5)["significance"]["significant"]
        assert not _result([CANDIDATE] * 14 + [BASELINE] * 6)["significance"]["significant"]


class TestRunComparisonAccounting:
    @staticmethod
    def _fake_run(choices, *, seen=None):
        def run_fn(baseline, candidate, *, cases, config):
            if seen is not None:
                seen.update({"baseline": baseline, "candidate": candidate, "cases": list(cases)})
            out = aggregate_choices(choices)
            out["per_case"] = [{"prompt": "p", "choice": c} for c in choices]
            return out

        return run_fn

    def test_dropped_cases_are_reported(self):
        """run_pairwise_eval silently skips empty/error responses. Four documented
        causes of empty-at-200 make that routine here, and an unreported drop turns
        a halved sample into an apparently clean one."""
        r = run_comparison(
            COMPARISONS[0],
            run_fn=self._fake_run([CANDIDATE] * 5),
            cases=[{"prompt": f"q{i}"} for i in range(12)],
            engines={"lite_agent": "1", "flash_agent": "2"},
        )
        assert r["n_selected"] == 12
        assert r["n_judged"] == 5
        assert r["dropped"] == 7

    def test_the_configured_engines_are_passed_in_the_right_order(self):
        seen: dict = {}
        run_comparison(
            COMPARISONS[1],
            run_fn=self._fake_run([TIE], seen=seen),
            cases=[{"prompt": "q"}],
            engines={"sonnet_agent": "SONNET", "pro_agent": "PRO"},
        )
        assert seen["baseline"].endswith("SONNET")
        assert seen["candidate"].endswith("PRO")

    def test_the_verdict_is_attached(self):
        r = run_comparison(
            COMPARISONS[0],
            run_fn=self._fake_run([CANDIDATE] * 16 + [BASELINE] * 3),
            cases=[{"prompt": f"q{i}"} for i in range(19)],
            engines={"lite_agent": "1", "flash_agent": "2"},
        )
        assert r["verdict"] == "CANDIDATE_BETTER"
        assert r["band"] == "medium"

    def test_a_win_on_an_already_fixed_miscut_confirms_rather_than_advises(self):
        """After the boundary moved, the bigger model winning CONFIRMS the shipped
        cut-point. Still saying "retune the boundary" would be advice to redo work
        already done — and would read as evidence the fix had not landed."""
        r = run_comparison(
            COMPARISONS[0],  # COMPLEXITY_LOW=0.25 < 0.40, so the miscut is fixed
            run_fn=self._fake_run([CANDIDATE] * 14 + [BASELINE] * 2),
            cases=[{"prompt": f"q{i}"} for i in range(16)],
            engines={"lite_agent": "1", "flash_agent": "2"},
        )
        assert r["verdict"] == "CANDIDATE_BETTER"
        assert r["miscut_active"] is False
        assert "retune" not in r["reading"]
        assert "confirms" in r["reading"]

    def test_a_win_on_a_live_miscut_still_advises_retuning(self, monkeypatch):
        monkeypatch.setattr("src.config.COMPLEXITY_LOW", 0.44)
        r = run_comparison(
            COMPARISONS[0],
            run_fn=self._fake_run([CANDIDATE] * 14 + [BASELINE] * 2),
            cases=[{"prompt": f"q{i}"} for i in range(16)],
            engines={"lite_agent": "1", "flash_agent": "2"},
        )
        assert r["miscut_active"] is True
        assert "retune" in r["reading"]

    def test_flip_and_sampling_defaults_are_the_rigorous_ones(self):
        """The run must record its own rigour: an accidental sampling_count=1 or
        flip=False would leave position bias in the number with no trace."""
        captured: dict = {}

        def run_fn(baseline, candidate, *, cases, config):
            captured["config"] = config
            return aggregate_choices([TIE])

        run_comparison(
            COMPARISONS[0],
            run_fn=run_fn,
            cases=[{"prompt": "q"}],
            engines={"lite_agent": "1", "flash_agent": "2"},
        )
        assert captured["config"].flip_enabled is True
        assert captured["config"].sampling_count == 4


class TestSubBandSplit:
    """A pooled win-rate is not evidence about a subset, and the high band is the
    live example: the router splits it at COMPLEXITY_HIGH and sends the two halves
    to different tiers, so 17-1 overall can coexist with the upper half going the
    other way."""

    @staticmethod
    def _result(entries):
        return {"per_case": [{"prompt": f"q{i}", **e} for i, e in enumerate(entries)]}

    def test_each_side_gets_its_own_significance(self):
        from src.eval.router_boundary_experiment import subband_split

        out = subband_split(
            self._result(
                [{"score": 0.75, "choice": CANDIDATE}] * 12
                + [{"score": 0.90, "choice": BASELINE}] * 4
            ),
            0.80,
        )
        assert out["below"]["wins"] == 12 and out["below"]["losses"] == 0
        assert out["at_or_above"]["wins"] == 0 and out["at_or_above"]["losses"] == 4
        assert out["below"]["significance"]["significant"] is True
        # 0-4 is a clean sweep the other way but n=4 cannot reach p<0.05.
        assert out["at_or_above"]["significance"]["significant"] is False

    def test_a_pooled_win_can_hide_a_reversed_subset(self):
        """THE reason this exists. Overall 12-4 for the candidate, but every one of
        the losses is on one side of the cut."""
        from src.eval.router_boundary_experiment import subband_split

        out = subband_split(
            self._result(
                [{"score": 0.75, "choice": CANDIDATE}] * 12
                + [{"score": 0.90, "choice": BASELINE}] * 4
            ),
            0.80,
        )
        assert out["below"]["significance"]["win_rate_decisive"] == 1.0
        assert out["at_or_above"]["significance"]["win_rate_decisive"] == 0.0

    def test_unscored_cases_are_counted_not_silently_dropped(self):
        from src.eval.router_boundary_experiment import subband_split

        out = subband_split(
            self._result([{"choice": CANDIDATE}, {"score": 0.9, "choice": CANDIDATE}]), 0.80
        )
        assert out["unscored"] == 1
        assert out["at_or_above"]["wins"] == 1

    def test_ties_count_toward_neither_side(self):
        from src.eval.router_boundary_experiment import subband_split

        out = subband_split(self._result([{"score": 0.9, "choice": TIE}] * 5), 0.80)
        assert out["at_or_above"]["significance"]["decisive"] == 0


class TestPerCaseAnnotation:
    def test_score_and_tier_are_attached(self):
        from types import SimpleNamespace

        from src.eval.router_boundary_experiment import annotate_per_case

        async def fake(_prompt):
            return SimpleNamespace(score=0.40, level="medium", reason="")

        per_case = [{"prompt": "q", "choice": CANDIDATE}]
        annotate_per_case(per_case, classify=fake)
        assert per_case[0]["score"] == 0.40
        assert per_case[0]["tier"] == "flash"

    def test_a_classifier_failure_never_costs_the_completed_run(self):
        """The expensive part (engines + judge) is already paid for by this point.
        Losing annotation must degrade, not raise."""
        from src.eval.router_boundary_experiment import annotate_per_case

        async def boom(_prompt):
            raise RuntimeError("classifier down")

        per_case = [{"prompt": "q", "choice": CANDIDATE}]
        annotate_per_case(per_case, classify=boom)
        assert "score" not in per_case[0]
        assert "classifier down" in per_case[0]["annotation_error"]


class TestReportAndCli:
    def test_report_names_the_verdict_and_the_drop_count(self):
        r = run_comparison(
            COMPARISONS[0],
            run_fn=TestRunComparisonAccounting._fake_run([CANDIDATE] * 3 + [BASELINE] * 3),
            cases=[{"prompt": f"q{i}"} for i in range(19)],
            engines={"lite_agent": "1", "flash_agent": "2"},
        )
        text = format_report([r])
        assert "INCONCLUSIVE" in text
        assert "dropped" in text
        assert "decisive cases to settle" in text

    def test_dry_run_spends_nothing_and_names_both_comparisons(self, capsys):
        assert main(["--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "medium" in out and "high" in out
        assert "lite_agent -> flash_agent" in out
        assert "sonnet_agent -> pro_agent" in out

    def test_dry_run_shows_the_case_counts_the_power_claim_rests_on(self, capsys):
        main(["--dry-run"])
        out = capsys.readouterr().out
        assert "19 cases" in out and "32 cases" in out

    def test_band_filter_selects_one_comparison(self, capsys):
        main(["--dry-run", "--band", "high"])
        out = capsys.readouterr().out
        assert "sonnet_agent" in out and "lite_agent" not in out

    def test_a_dead_engine_aborts_before_spending(self, monkeypatch, capsys):
        """The tier ids in .env have all named deleted engines before. Failing deep
        inside an eval means paying for the run first."""
        monkeypatch.setattr(
            "src.eval.cross_model_experiment.preflight_engines",
            lambda agents: ["lite_agent: LITE_ENGINE_ID=1 does not resolve to a live engine"],
        )
        assert main([]) == 1
        assert "do not resolve" in capsys.readouterr().out


def test_comparison_is_immutable():
    """The orientation is the experiment's single most inversion-prone fact."""
    with pytest.raises(dataclasses.FrozenInstanceError):
        COMPARISONS[0].baseline_agent = "pro_agent"  # ty: ignore[invalid-assignment]
    assert isinstance(COMPARISONS[0], Comparison)
