"""The eval rubrics and their reference standard must describe the REAL system.

Two defects motivated this file, both of which made a judge score confidently
against a policy the system does not implement:

1. The calibration gold set's 5/5 "correct" responses cited invented policy — a
   $200 entertainment limit (real: $150), city-specific lodging caps, a "gifts"
   category, per-person meal scaling, travel-class rules. Calibrating the judge
   against it pushed toward *rewarding* hallucinated policy, and it dragged the
   gate to 68.8% (FAIL). Correcting the reference alone took it to 96.9%.

2. The tool-use rubric asked "did the agent call the right tool?" while
   ``run_inference`` gives it text only, no trajectory — so it graded *narration*.
   The identical correct answer scored 0.2 or 1.0 depending purely on whether it
   said "I checked the policy".

These are content assertions on committed strings and JSON: no GCP, no judge
calls. They are cheap precisely because the failures they catch are expensive.
"""

import json
import re
from pathlib import Path

import pytest

from src.eval.batch_eval import POLICY_COMPLIANCE_METRIC, TOOL_USE_METRIC
from src.mcp_servers.expense.mock_db import POLICY_LIMITS

_REPO_ROOT = Path(__file__).resolve().parents[1]
GOLD_PATH = _REPO_ROOT / "src/eval/data/policy_calibration_gold.json"


@pytest.fixture(scope="module")
def gold():
    return json.loads(GOLD_PATH.read_text())


def _dollars(text: str) -> set[str]:
    return set(re.findall(r"\$(\d+(?:\.\d+)?)", text))


class TestPolicyRubricMatchesTheSystem:
    def test_every_real_limit_is_declared(self):
        """A limit the rubric omits is one the judge cannot check."""
        instruction = str(POLICY_COMPLIANCE_METRIC.prompt_template)
        for category, limit in POLICY_LIMITS.items():
            assert category in instruction.lower(), category
            assert str(int(limit)) in instruction, f"{category} limit {limit}"

    def test_no_invented_limits_are_declared(self):
        """The rubric must not teach the judge a limit the system doesn't enforce."""
        instruction = str(POLICY_COMPLIANCE_METRIC.prompt_template)
        real = {str(int(v)) for v in POLICY_LIMITS.values()}
        # Only the category limits should appear as dollar figures.
        assert _dollars(instruction) <= real

    def test_non_expense_requests_are_explicitly_out_of_scope(self):
        """The fix for a hotel SEARCH scoring 0.2 for "no policy awareness" —
        the agent has nothing to check, so silence about limits is correct."""
        instruction = str(POLICY_COMPLIANCE_METRIC.prompt_template).lower()
        assert "scope" in instruction
        assert "search" in instruction
        assert "missing" in instruction, "a reply asking for the amount must not be penalised"

    def test_over_limit_expenses_are_still_submitted(self):
        """The coordinator's spec calls refusing-to-submit a defect; the rubric
        has to agree or it grades the agent against a different product."""
        instruction = str(POLICY_COMPLIANCE_METRIC.prompt_template).lower()
        assert "refus" in instruction


class TestToolUseRubricGradesWhatItCanSee:
    def test_it_states_that_the_trajectory_is_not_available(self):
        instruction = str(TOOL_USE_METRIC.prompt_template).lower()
        assert "cannot see the tool calls" in instruction

    def test_it_forbids_rewarding_narration(self):
        """The defect: identical answers scored 0.2 vs 1.0 on whether the agent
        said "I checked the policy". That grades writing style, not tool use."""
        instruction = str(TOOL_USE_METRIC.prompt_template).lower()
        assert "narrat" in instruction

    def test_calling_no_tool_can_be_correct(self):
        """A greeting or a request for a missing detail needs no tool; the old
        rating scale had no way to say so ("1 = No tool called when one was needed")."""
        template = str(TOOL_USE_METRIC.prompt_template).lower()
        assert "no tool" in template

    def test_it_no_longer_asks_for_unobservable_call_ordering(self):
        """ "Did the agent call check_expense_policy BEFORE submit_expense" is not
        answerable from response text — that question belongs to a trajectory
        metric (src/eval/tool_faithfulness.py), not this one."""
        template = str(TOOL_USE_METRIC.prompt_template)
        assert "BEFORE" not in template


class TestCalibrationGoldIsGroundedInRealPolicy:
    """THE test that would have caught the original defect.

    Every dollar figure in a reference response must be either an amount the
    prompt itself named or a real policy limit. Anything else is invented policy
    presented to the judge as ground truth.
    """

    def test_reference_responses_cite_only_real_limits(self, gold):
        real = {str(int(v)) for v in POLICY_LIMITS.values()}
        offenders = []
        for case in gold["cases"]:
            # Amounts derivable by arithmetic (an overage = amount - limit) are
            # legitimate; only a figure presented as a POLICY RULE is invented.
            derived = {
                str(int(float(a) - v))
                for a in _dollars(case["prompt"])
                for v in POLICY_LIMITS.values()
                if float(a) > v
            }
            allowed = real | _dollars(case["prompt"]) | derived
            invented = {d for d in _dollars(case["response"]) if d.split(".")[0] not in allowed}
            # Only the 5/5 references define "correct"; low-scored responses are
            # *meant* to contain wrong policy, that's what makes them low.
            if case.get("human_score") == 5 and invented:
                offenders.append((case["prompt"], sorted(invented)))
        assert not offenders, f"5/5 references citing non-existent limits: {offenders}"

    def test_no_reference_invents_a_category(self, gold):
        """v1 had a "gifts policy"; the system has exactly five categories."""
        known = set(POLICY_LIMITS)
        for case in gold["cases"]:
            if case.get("human_score") != 5:
                continue
            text = case["response"].lower()
            for word in ("gifts policy", "gift policy"):
                assert word not in text, case["prompt"]
            # Any "<x> limit/cap" phrase must name a real category.
            for named in re.findall(r"(\w+)\s+(?:limit|cap)\b", text):
                if named in {"the", "this", "policy", "expense", "per", "a", "no", "category"}:
                    continue
                assert named in known or f"{named}s" in known, (
                    f"{case['prompt']}: unknown category '{named}'"
                )

    def test_no_reference_treats_refusal_to_submit_as_correct(self, gold):
        """The coordinator spec: an over-limit expense is submitted and flagged.
        v1 scored a refusal 5/5, contradicting the product it grades."""
        for case in gold["cases"]:
            if case.get("human_score") != 5 or not case["prompt"].lower().startswith("submit"):
                continue
            text = case["response"].lower()
            assert not re.search(r"\b(can't|cannot|won't) submit\b", text), case["prompt"]

    def test_provenance_records_the_correction(self, gold):
        """The gold set is a human-label artefact; a silent rewrite of one is not
        acceptable, so the correction has to be on the record."""
        assert gold["version"] != "1"
        assert "CORRECTED" in gold["provenance"]
        assert "mock_db" in gold["provenance"], "must name the ground-truth source"

    def test_every_case_still_has_a_paired_good_and_bad_response(self, gold):
        """The set's discriminative power comes from pairs: the same prompt with a
        good and a degraded response. A correction must not collapse a pair."""
        from collections import defaultdict

        by_prompt = defaultdict(list)
        for case in gold["cases"]:
            by_prompt[case["prompt"]].append(case["human_score"])
        for prompt, scores in by_prompt.items():
            assert len(scores) >= 2, prompt
            assert max(scores) > min(scores), f"{prompt}: no good/bad contrast"
