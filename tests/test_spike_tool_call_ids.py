"""Offline tests for the tool-call-id spike (history scan + verdict logic).

No Vertex, no engine — the spike's two pure helpers carry the reasoning that
decides whether a run proved anything, so they are the parts worth pinning.
"""

from types import SimpleNamespace

import pytest

from src.eval import spike_tool_call_ids as spike


def _part(*, call=None, resp=None):
    return SimpleNamespace(
        function_call=SimpleNamespace(id=call) if call is not None else None,
        function_response=SimpleNamespace(id=resp) if resp is not None else None,
    )


def _session(*parts_per_event):
    return SimpleNamespace(
        events=[
            SimpleNamespace(content=SimpleNamespace(parts=list(parts))) for parts in parts_per_event
        ]
    )


class TestHistoryIds:
    def test_collects_calls_and_responses_in_order(self):
        session = _session(
            [_part(call="adk-1")],
            [_part(resp="adk-1")],
        )
        assert spike._history_ids(session) == [("call", "adk-1"), ("resp", "adk-1")]

    def test_a_part_carrying_both_yields_both(self):
        session = _session([_part(call="toolu_a", resp="toolu_a")])
        assert spike._history_ids(session) == [("call", "toolu_a"), ("resp", "toolu_a")]

    def test_text_only_events_contribute_nothing(self):
        session = _session([SimpleNamespace(function_call=None, function_response=None)])
        assert spike._history_ids(session) == []

    def test_an_event_with_no_content_is_skipped(self):
        """A user turn has content but ADK also emits content-less events."""
        session = SimpleNamespace(events=[SimpleNamespace(content=None)])
        assert spike._history_ids(session) == []

    def test_a_session_with_no_events_is_empty_not_an_error(self):
        assert spike._history_ids(SimpleNamespace(events=None)) == []
        assert spike._history_ids(SimpleNamespace()) == []


class TestVerdict:
    @pytest.mark.parametrize("pre_fix", ["RAISED", "EMPTY"])
    def test_pass_when_the_fix_rescues_a_failing_arm(self, pre_fix):
        message, code = spike.verdict(pre_fix=pre_fix, fixed="FULL")
        assert code == 0
        assert "PASS" in message

    def test_a_healthy_pre_fix_arm_is_inconclusive_not_a_pass(self):
        """The repro must actually fail first, or the run proves nothing.

        A pre-fix arm that answered normally means no ``adk-`` id reached the
        Claude turn, so the fixed arm's success is not evidence about the fix.
        """
        message, code = spike.verdict(pre_fix="FULL", fixed="FULL")
        assert code == 2
        assert "INCONCLUSIVE" in message

    @pytest.mark.parametrize("fixed", ["RAISED", "EMPTY"])
    def test_fail_when_the_fixed_arm_still_breaks(self, fixed):
        message, code = spike.verdict(pre_fix="RAISED", fixed=fixed)
        assert code == 1
        assert "FAIL" in message
        assert fixed in message
