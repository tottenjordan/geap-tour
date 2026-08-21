"""Tests for the router health verifier's pure layer (no GCP, no engine).

The live probe needs a deployed engine; everything that decides PASS/FAIL is
pure and tested here with synthetic event lists.
"""

from __future__ import annotations

import pytest

from src.eval import verify_router_health as vrh


def _ev(*, text=None, call=None, response=None, author="router_agent"):
    """One stream event in the shape the raw-SSE client returns."""
    part: dict = {}
    if text is not None:
        part["text"] = text
    if call is not None:
        part["function_call"] = {"name": call}
    if response is not None:
        part["function_response"] = {"name": response}
    return {"author": author, "content": {"role": "model", "parts": [part]}}


class TestClassifyOutcome:
    def test_text_is_full(self):
        assert vrh.classify_outcome([_ev(text="Booked FL001 for you.")]) == "FULL"

    def test_no_events_is_empty(self):
        assert vrh.classify_outcome([]) == "EMPTY"

    def test_tool_calls_without_text_is_still_empty(self):
        """The exact live failure: both MCP calls ran, zero characters returned."""
        events = [
            _ev(call="booking_mcp_book_flight"),
            _ev(response="booking_mcp_book_flight"),
            _ev(call="search_mcp_search_hotels"),
            _ev(response="search_mcp_search_hotels"),
        ]
        assert vrh.classify_outcome(events) == "EMPTY"

    def test_whitespace_only_is_empty(self):
        assert vrh.classify_outcome([_ev(text="   \n ")]) == "EMPTY"

    def test_throttle_marker_is_reported_separately(self):
        from src.models.quota_retry import THROTTLED_RESPONSE_PREFIX

        assert vrh.classify_outcome([_ev(text=THROTTLED_RESPONSE_PREFIX + " …")]) == "THROTTLED"

    def test_empty_marker_is_reported_separately(self):
        """A labelled empty is an infra failure the user CAN see — not silence."""
        from src.models.quota_retry import EMPTY_RESPONSE_PREFIX

        assert vrh.classify_outcome([_ev(text=EMPTY_RESPONSE_PREFIX + " …")]) == "EMPTY_LABELLED"


class TestSummarize:
    def _results(self, outcomes, tier="lite"):
        return [
            {"tier": tier, "outcome": o, "chars": 10, "latency_s": 1.0, "prompt": "p"}
            for o in outcomes
        ]

    def test_counts_and_rates(self):
        s = vrh.summarize(self._results(["FULL", "FULL", "FULL", "EMPTY"]))
        assert s["n"] == 4
        assert s["counts"]["FULL"] == 3
        assert s["counts"]["EMPTY"] == 1
        assert s["empty_rate"] == pytest.approx(0.25)
        assert s["full_rate"] == pytest.approx(0.75)

    def test_silent_empties_are_counted_apart_from_labelled_ones(self):
        """A labelled empty still failed, but it is *diagnosable* — the whole
        point of the retry wrapper's last-resort message. Track both."""
        s = vrh.summarize(self._results(["FULL", "EMPTY", "EMPTY_LABELLED", "THROTTLED"]))
        assert s["silent_empty"] == 1
        assert s["labelled_failure"] == 2
        assert s["empty_rate"] == pytest.approx(0.25)  # only the SILENT one

    def test_reports_a_confidence_interval(self):
        s = vrh.summarize(self._results(["FULL"] * 23 + ["EMPTY"]))
        lo, hi = s["empty_rate_ci"]
        assert 0.0 <= lo <= s["empty_rate"] <= hi <= 1.0
        # 1/24 is genuinely imprecise — the CI must say so rather than imply 4%.
        assert hi > 0.15

    def test_per_tier_breakdown(self):
        results = self._results(["FULL", "EMPTY"], tier="lite") + self._results(
            ["FULL", "FULL"], tier="opus"
        )
        s = vrh.summarize(results)
        assert s["by_tier"]["lite"]["empty_rate"] == pytest.approx(0.5)
        assert s["by_tier"]["opus"]["empty_rate"] == pytest.approx(0.0)

    def test_empty_input_does_not_divide_by_zero(self):
        s = vrh.summarize([])
        assert s["n"] == 0
        assert s["empty_rate"] == 0.0

    def test_latency_percentiles_use_successful_turns_only(self):
        results = [
            {"tier": "lite", "outcome": "FULL", "chars": 5, "latency_s": 2.0, "prompt": "p"},
            {"tier": "lite", "outcome": "EMPTY", "chars": 0, "latency_s": 99.0, "prompt": "p"},
        ]
        s = vrh.summarize(results)
        assert s["p50_latency_s"] == pytest.approx(2.0)


class TestSkippedTurns:
    """A turn we never got to send is not evidence about the router.

    The first live 28-turn run died at turn 18 on a ``create_session`` hiccup
    (``KeyError: 'output'``), throwing away the whole measurement. A skipped turn
    is now recorded, excluded from the rates, and reported — counting it as an
    empty would blame the router for a control-plane blip, and dropping it
    silently would make a half-finished run look complete.
    """

    def _mixed(self):
        return [
            {"tier": "lite", "outcome": "FULL", "chars": 5, "latency_s": 1.0, "prompt": "p"},
            {"tier": "lite", "outcome": "EMPTY", "chars": 0, "latency_s": 2.0, "prompt": "p"},
            {"tier": "lite", "outcome": "SKIPPED", "chars": 0, "latency_s": 0.0, "prompt": "p"},
        ]

    def test_skipped_turns_are_excluded_from_the_rates(self):
        s = vrh.summarize(self._mixed())
        assert s["n"] == 2  # not 3
        assert s["empty_rate"] == pytest.approx(0.5)

    def test_skipped_turns_are_reported_not_dropped(self):
        assert vrh.summarize(self._mixed())["skipped"] == 1

    def test_a_run_that_is_all_skips_never_passes(self):
        results = [{"tier": "lite", "outcome": "SKIPPED", "chars": 0, "latency_s": 0.0}] * 5
        s = vrh.summarize(results)
        assert s["n"] == 0
        assert vrh.verdict(s, threshold=0.5)["passed"] is False

    def test_a_session_failure_skips_that_turn_instead_of_aborting_the_run(self):
        """One bad session must not cost the other 27 turns."""
        calls = {"n": 0}

        def flaky_session(resource, user_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyError("output")
            return "sess-1"

        results = vrh.run_probes(
            "res",
            repeat=1,
            probes=[("lite", "a"), ("lite", "b")],
            session_fn=flaky_session,
            stream_fn=lambda *a, **k: [_ev(text="hi")],
            sleep=lambda _s: None,
            verbose=False,
        )

        assert [r["outcome"] for r in results] == ["SKIPPED", "FULL"]

    def test_a_stream_failure_also_skips_rather_than_counting_as_empty(self):
        def boom(*a, **k):
            raise RuntimeError("transport reset")

        results = vrh.run_probes(
            "res",
            repeat=1,
            probes=[("lite", "a")],
            session_fn=lambda *a, **k: "s",
            stream_fn=boom,
            sleep=lambda _s: None,
            verbose=False,
        )

        assert [r["outcome"] for r in results] == ["SKIPPED"]
        assert vrh.summarize(results)["n"] == 0


class TestVerdict:
    def test_passes_below_the_threshold(self):
        s = vrh.summarize([{"tier": "lite", "outcome": "FULL", "chars": 1, "latency_s": 1.0}] * 10)
        assert vrh.verdict(s, threshold=0.1)["passed"] is True

    def test_fails_above_the_threshold(self):
        results = [{"tier": "lite", "outcome": "EMPTY", "chars": 0, "latency_s": 1.0}] * 10
        assert vrh.verdict(vrh.summarize(results), threshold=0.1)["passed"] is False

    def test_a_zero_sample_run_never_reports_a_pass(self):
        """No data is not a green light."""
        assert vrh.verdict(vrh.summarize([]), threshold=0.1)["passed"] is False

    def test_report_renders_without_crashing(self):
        s = vrh.summarize([{"tier": "lite", "outcome": "FULL", "chars": 1, "latency_s": 1.0}])
        text = vrh.format_report(s, vrh.verdict(s, threshold=0.1))
        assert "ROUTER HEALTH" in text
        assert "empty" in text.lower()
