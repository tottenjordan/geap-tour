"""Offline tests for the trajectory-visibility spike (verdict logic + fallback)."""

import pytest

from src.eval import spike_trajectory_visibility as spike


def _fc_event(name, author="coordinator_agent"):
    return {"author": author, "content": {"parts": [{"function_call": {"name": name}}]}}


def _text_event(text, author="coordinator_agent"):
    return {"author": author, "content": {"parts": [{"text": text}]}}


class TestDescribeEventsVerdict:
    def test_branch_a_when_domain_calls_visible(self, capsys):
        events = [_fc_event("book_flight"), _fc_event("search_hotels"), _text_event("done")]
        spike.describe_events(events)
        assert "Branch A" in capsys.readouterr().out

    def test_branch_b_when_only_transfer(self, capsys):
        spike.describe_events([_fc_event("transfer_to_agent")])
        assert "Branch B" in capsys.readouterr().out

    def test_inconclusive_when_no_calls(self, capsys):
        spike.describe_events([_text_event("hello")])
        assert "inconclusive" in capsys.readouterr().out


class _HappyEngine:
    def stream_query(self, **_):
        yield _fc_event("book_flight")


class _NonSkewEngine:
    def stream_query(self, **_):
        raise ValueError("some unrelated value error")
        yield  # pragma: no cover - marks this a generator


class TestStreamEventsFallback:
    def test_happy_path_returns_events(self):
        events = spike.stream_events(_HappyEngine(), "hi", user_id="u")
        assert spike._part_summary  # module intact
        assert events and events[0]["content"]["parts"][0]["function_call"]["name"] == "book_flight"

    def test_non_skew_value_error_propagates(self):
        # A ValueError that isn't the SSE array-parse skew must not be swallowed.
        with pytest.raises(ValueError, match="unrelated"):
            spike.stream_events(_NonSkewEngine(), "hi", user_id="u")
