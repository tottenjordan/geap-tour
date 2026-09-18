"""A degradation that silently does nothing is worse than no degradation at all.

These exist to validate the multi-turn rubrics, by scoring a real conversation
against deliberately broken copies of it. The whole method rests on one assumption:
that the mutation actually happened. If `drop_tool_calls` quietly returns the
conversation unchanged, the variant scores identically to the original and that
reads as *rubric blindness* — a wrong and expensive conclusion about someone else's
code.

So every test here asserts the defect was injected, and that the parts which must
survive did.
"""

from __future__ import annotations

import copy

import pytest

from src.eval.multi_turn_degrade import (
    DEGRADATIONS,
    STONEWALL_TEXT,
    TARGETS,
    degrade,
    describe,
)


def _conversation() -> dict:
    """Two turns, each: a user message, a tool call + response, then an answer."""

    def turn(i: int, user: str, tool: str, answer: str) -> dict:
        return {
            "turn_index": i,
            "turn_id": f"inv-{i}",
            "events": [
                {
                    "author": "user",
                    "content": {"role": "user", "parts": [{"text": user}]},
                    "invocation_id": f"inv-{i}",
                },
                {
                    "author": "coordinator_agent",
                    "content": {"role": "model", "parts": [{"function_call": {"name": tool}}]},
                    "invocation_id": f"inv-{i}",
                },
                {
                    "author": "coordinator_agent",
                    "content": {
                        "role": "user",
                        "parts": [{"function_response": {"name": tool, "response": {}}}],
                    },
                    "invocation_id": f"inv-{i}",
                },
                {
                    "author": "coordinator_agent",
                    "content": {"role": "model", "parts": [{"text": answer}]},
                    "invocation_id": f"inv-{i}",
                },
            ],
        }

    return {
        "turns": [
            turn(0, "Find flights to JFK", "search_mcp_search_flights", "I found two flights."),
            turn(1, "Book the second one", "booking_mcp_book_flight", "Booked, ref FL002."),
        ],
        "transcript": [],
        "stopped": "plan_complete",
        "turn_count": 2,
        "tool_calls": ["search_mcp_search_flights", "booking_mcp_book_flight"],
    }


class TestTheMutationActuallyHappens:
    """THE precondition. A no-op degradation masquerades as a blind rubric."""

    @pytest.mark.parametrize("name", sorted(DEGRADATIONS))
    def test_every_degradation_changes_the_conversation(self, name):
        original = _conversation()
        assert degrade(original, name) != original, f"{name} was a no-op"

    @pytest.mark.parametrize("name", sorted(DEGRADATIONS))
    def test_the_original_is_never_mutated(self, name):
        """Variants are scored against the original in the same run; mutating it in
        place would compare the original to itself and show zero discrimination."""
        original = _conversation()
        snapshot = copy.deepcopy(original)
        degrade(original, name)
        assert original == snapshot

    def test_an_unknown_name_raises_rather_than_silently_passing_through(self):
        with pytest.raises(KeyError, match="unknown degradation"):
            degrade(_conversation(), "typo_here")


class TestDropToolCalls:
    def test_it_removes_every_tool_call_and_response(self):
        d = describe(degrade(_conversation(), "drop_tool_calls"))
        assert d["tool_calls"] == 0
        assert d["tool_responses"] == 0

    def test_the_agent_still_claims_the_work(self):
        """The point of this variant: the text still says 'Booked, ref FL002' while
        no booking call exists. A rubric reading only the text cannot tell."""
        bad = degrade(_conversation(), "drop_tool_calls")
        text = str(bad)
        assert "Booked, ref FL002." in text
        assert "booking_mcp_book_flight" not in text

    def test_the_user_turns_survive(self):
        before, after = (
            describe(_conversation()),
            describe(degrade(_conversation(), "drop_tool_calls")),
        )
        assert after["user_events"] == before["user_events"]


class TestStonewall:
    def test_every_agent_answer_is_replaced(self):
        bad = degrade(_conversation(), "stonewall")
        texts = [
            p["text"]
            for t in bad["turns"]
            for e in t["events"]
            if e.get("author") != "user"
            for p in e["content"]["parts"]
            if "text" in p
        ]
        assert texts and all(t == STONEWALL_TEXT for t in texts)

    def test_no_task_progress_remains(self):
        d = describe(degrade(_conversation(), "stonewall"))
        assert d["tool_calls"] == 0

    def test_it_is_fluent_not_gibberish(self):
        """A rubric that only catches word salad has been tested against nothing a
        real degraded agent would produce."""
        assert len(STONEWALL_TEXT.split()) > 15
        assert STONEWALL_TEXT[0].isupper() and STONEWALL_TEXT.endswith("?")


class TestAbandonMidway:
    def test_the_first_turn_is_untouched(self):
        bad = degrade(_conversation(), "abandon_midway")
        assert bad["turns"][0] == _conversation()["turns"][0]

    def test_later_turns_keep_the_user_and_lose_the_agent(self):
        bad = degrade(_conversation(), "abandon_midway")
        later = bad["turns"][1]["events"]
        assert later, "the user's turn must remain — the agent went silent, not the user"
        assert all(e["author"] == "user" for e in later)

    def test_it_differs_from_stonewalling(self):
        """Silence and useless replies are different failures; a rubric scoring them
        identically is not reading turn structure."""
        assert degrade(_conversation(), "abandon_midway") != degrade(_conversation(), "stonewall")


class TestScrambleTurns:
    def test_the_agent_blocks_are_reversed_against_the_users(self):
        bad = degrade(_conversation(), "scramble_turns")
        first_answer = [
            p["text"]
            for e in bad["turns"][0]["events"]
            if e.get("author") != "user"
            for p in e["content"]["parts"]
            if "text" in p
        ]
        assert first_answer == ["Booked, ref FL002."], "turn 0 should now carry turn 1's reply"

    def test_nothing_is_lost_only_misplaced(self):
        """The distinguishing property: same events, wrong order. If content went
        missing, a low score could be explained by the loss instead of the
        incoherence."""
        before, after = (
            describe(_conversation()),
            describe(degrade(_conversation(), "scramble_turns")),
        )
        assert after["agent_events"] == before["agent_events"]
        assert after["tool_calls"] == before["tool_calls"]
        assert after["user_events"] == before["user_events"]

    def test_a_single_turn_conversation_is_left_alone(self):
        """Nothing to misalign. The caller must read an unchanged conversation as
        UNTESTED, not as a passing rubric."""
        one = {"turns": [_conversation()["turns"][0]]}
        assert degrade(one, "scramble_turns") == one


class TestEachDegradationNamesItsTarget:
    def test_every_degradation_has_a_target_rubric(self):
        """A flat score should localise the blindness, not just report one."""
        assert set(TARGETS) == set(DEGRADATIONS)

    def test_the_targets_are_real_multi_turn_rubrics(self):
        from src.eval.agent_eval_configs import get_multi_turn_metrics

        names = {getattr(m, "name", str(m)).lower() for m in get_multi_turn_metrics()}
        for degradation, target in TARGETS.items():
            assert any(target in n or n in target for n in names), (
                f"{degradation} targets {target!r}, which is not a metric this suite scores"
            )
