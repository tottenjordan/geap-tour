"""The blind annotation CLI, and the human-ceiling maths it feeds.

The properties worth testing here are the ones that make a second annotation pass
*mean* something: that it is blind (no existing score leaks into what the
annotator sees), grounded in the real policy table, resumable, and non-destructive
until an explicit merge. Every dependency is injected, so nothing touches stdin.
"""

import pytest

from src.eval import annotate as an
from src.eval import calibration as cal


def _case(prompt="p", response="r", score=None, difficulty="hard"):
    case = {"prompt": prompt, "response": response, "difficulty": difficulty}
    if score is not None:
        case["annotations"] = {"a1": score}
        case["human_score"] = score
    return case


class TestBlindness:
    """Anchoring is the whole risk of a second pass by the same operator."""

    def test_the_rendered_case_never_shows_an_existing_score(self):
        case = _case("Is $50 ok?", "Yes, within the $75 limit.", score=5)
        rendered = an.render_case(case, 1, 10)
        assert "Is $50 ok?" in rendered
        assert "Yes, within the $75 limit." in rendered
        assert "5" not in rendered.replace("$50", "").replace("$75", "").replace("1/10", "")

    def test_it_does_not_show_other_annotators(self):
        case = _case(score=4)
        case["annotations"]["a2"] = 2
        rendered = an.render_case(case, 1, 1)
        assert "a1" not in rendered
        assert "a2" not in rendered


class TestGrounding:
    """v1's labels invented policy; the annotator must grade against the system."""

    def test_the_policy_table_comes_from_the_system(self):
        from src.mcp_servers.expense.mock_db import POLICY_LIMITS

        table = an.policy_table()
        for category, limit in POLICY_LIMITS.items():
            assert category in table
            assert f"{limit:,.0f}" in table

    def test_it_rules_out_the_rules_v1_invented(self):
        table = an.policy_table().lower()
        for phrase in ("no city caps", "per-person", "travel-class", "pre-approval"):
            assert phrase in table

    def test_it_states_the_submit_and_flag_rule(self):
        assert "still submitted" in an.policy_table().lower()


class TestResumability:
    def test_already_scored_cases_are_skipped(self):
        cases = [_case("p1", "r1"), _case("p2", "r2")]
        progress = {an.case_key(cases[0]): {"score": 4, "note": ""}}
        todo = an.pending(cases, progress, "a2")
        assert [c["prompt"] for c in todo] == ["p2"]

    def test_ordering_is_stable_for_one_annotator(self):
        """A resumed session must not reshuffle, or the annotator loses their place."""
        cases = [_case(f"p{i}", f"r{i}") for i in range(12)]
        assert an.pending(cases, {}, "a2") == an.pending(cases, {}, "a2")

    def test_two_annotators_see_different_orders(self):
        """Correlated fatigue on the same cases would inflate apparent agreement."""
        cases = [_case(f"p{i}", f"r{i}") for i in range(12)]
        assert an.pending(cases, {}, "a1") != an.pending(cases, {}, "a2")

    def test_a_case_is_keyed_by_prompt_and_response(self):
        """The gold set repeats prompts with different responses — keying on the
        prompt alone would silently collapse a good/bad pair into one."""
        a, b = _case("same", "good"), _case("same", "bad")
        assert an.case_key(a) != an.case_key(b)


class TestParseInput:
    @pytest.mark.parametrize("raw,score", [("4", 4), ("1", 1), ("5", 5), (" 3 ", 3)])
    def test_accepts_a_bare_score(self, raw, score):
        assert an.parse_input(raw) == (score, "")

    def test_accepts_a_score_with_a_note(self):
        assert an.parse_input("3 right limit, vague verdict") == (3, "right limit, vague verdict")

    @pytest.mark.parametrize("raw", ["", "0", "6", "abc", "-1", "4.5"])
    def test_rejects_anything_that_is_not_1_to_5(self, raw):
        """Rejected input must re-prompt, never record a wrong number."""
        assert an.parse_input(raw)[0] is None


class TestMerge:
    def test_merge_adds_the_annotator_and_recomputes_consensus(self):
        gold = {"cases": [_case("p", "r", score=5)]}
        progress = {an.case_key(gold["cases"][0]): {"score": 3, "note": "vague"}}
        merged, applied = an.merge_annotations(gold, "a2", progress)
        case = merged["cases"][0]
        assert applied == 1
        assert case["annotations"] == {"a1": 5, "a2": 3}
        assert case["human_score"] == 4  # median of 5 and 3
        assert case["notes"]["a2"] == "vague"

    def test_merge_leaves_unannotated_cases_alone(self):
        gold = {"cases": [_case("p", "r", score=5), _case("other", "r2")]}
        merged, applied = an.merge_annotations(gold, "a2", {})
        assert applied == 0
        assert "annotations" not in merged["cases"][1] or not merged["cases"][1]["annotations"]

    def test_a_session_does_not_mutate_the_gold_set(self, tmp_path, monkeypatch):
        """Merging is a separate, reviewable step — an in-progress pass must never
        touch the committed reference."""
        saved = {}
        cases = [_case("p1", "r1")]
        an.run_session(
            "a2",
            cases,
            {},
            input_fn=lambda _p: "4",
            output_fn=lambda _m: None,
            save_fn=lambda who, prog: saved.update({who: dict(prog)}),
        )
        assert saved["a2"]  # progress went to the session file...
        assert "annotations" not in cases[0]  # ...and not into the case


class TestSession:
    def test_it_records_score_and_note_and_saves_each_case(self):
        cases = [_case("p1", "r1"), _case("p2", "r2")]
        saves = []
        progress = an.run_session(
            "a2",
            cases,
            {},
            input_fn=lambda _p: "4 fine",
            output_fn=lambda _m: None,
            save_fn=lambda _w, prog: saves.append(len(prog)),
        )
        assert len(progress) == 2
        assert all(e == {"score": 4, "note": "fine"} for e in progress.values())
        assert saves == [1, 2], "must save after EVERY case, not at the end"

    def test_quitting_keeps_what_was_scored(self):
        cases = [_case("p1", "r1"), _case("p2", "r2")]
        answers = iter(["4", "q"])
        progress = an.run_session(
            "a2",
            cases,
            {},
            input_fn=lambda _p: next(answers),
            output_fn=lambda _m: None,
            save_fn=lambda *_a: None,
        )
        assert len(progress) == 1

    def test_bad_input_reprompts_instead_of_recording(self):
        answers = iter(["nine", "4"])
        progress = an.run_session(
            "a2",
            [_case("p1", "r1")],
            {},
            input_fn=lambda _p: next(answers),
            output_fn=lambda _m: None,
            save_fn=lambda *_a: None,
        )
        assert next(iter(progress.values()))["score"] == 4


class TestHumanCeiling:
    def test_alpha_is_nan_with_a_single_annotator(self):
        """No ceiling exists yet — the code must say so, not invent a number."""
        import math

        rel = cal.annotator_reliability([_case("p", "r", score=5), _case("q", "s", score=2)])
        assert math.isnan(rel["alpha"])
        assert rel["n_annotators"] == 1

    def test_alpha_is_one_when_two_annotators_agree_exactly(self):
        cases = [
            {"prompt": "p", "response": "r", "annotations": {"a1": 5, "a2": 5}},
            {"prompt": "q", "response": "s", "annotations": {"a1": 2, "a2": 2}},
        ]
        assert cal.annotator_reliability(cases)["alpha"] == pytest.approx(1.0)

    def test_consensus_is_the_median_not_the_mean(self):
        """One outlying annotator must not drag the reference."""
        case = {"prompt": "p", "response": "r", "annotations": {"a1": 5, "a2": 5, "a3": 1}}
        assert cal.consensus_score(case) == 5

    def test_disagreements_are_surfaced_worst_first(self):
        cases = [
            {"prompt": "small", "response": "r", "annotations": {"a1": 4, "a2": 5}},
            {"prompt": "big", "response": "r", "annotations": {"a1": 1, "a2": 5}},
        ]
        out = cal.annotator_disagreements(cases, min_delta=1.0)
        assert [c["prompt"] for c in out] == ["big", "small"]

    def test_unscored_cases_are_excluded_not_zeroed(self):
        """A case awaiting annotation is missing evidence; scoring it 0 would
        silently drag every aggregate."""
        cases = [_case("p", "r", score=5), _case("unscored", "r2")]
        assert [c["prompt"] for c in cal.scored_cases(cases)] == ["p"]

    def test_human_axis_refuses_an_unscored_case(self):
        with pytest.raises(ValueError, match="no annotations"):
            cal._human_axis(_case("unscored", "r"))


class TestCeilingVerdict:
    def test_judge_below_the_ceiling_names_the_judge(self):
        assert "BELOW" in cal.ceiling_verdict(0.60, 0.90)

    def test_judge_at_the_ceiling_is_as_good_as_the_labels_allow(self):
        assert "AT the human ceiling" in cal.ceiling_verdict(0.88, 0.90)

    def test_judge_above_the_ceiling_is_flagged_as_suspicious(self):
        """Beating human agreement is not a triumph — it usually means the judge
        has fitted one annotator rather than the rubric."""
        verdict = cal.ceiling_verdict(0.99, 0.70)
        assert "EXCEEDS" in verdict
        assert "suspicious" in verdict

    def test_missing_ceiling_says_so(self):
        assert "unavailable" in cal.ceiling_verdict(0.9, float("nan"))
