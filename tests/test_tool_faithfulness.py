"""Offline tests for the tool-call faithfulness evaluator (no live GCP).

Everything is exercised with fake events, a fake engine, and an injected judge
``generate_fn`` so the capture → prompt → score → CLI path is verified without
any network, credentials, or metric writes.
"""

import json

import pytest

from src.eval.tool_faithfulness import (
    _format_actual_tools,
    build_faithfulness_prompt,
    capture_interaction,
    main,
    parse_faithfulness_score,
    parse_hallucinated_actions,
    run_tool_faithfulness_eval,
    score_cases,
    select_faithfulness_cases,
)


# --------------------------------------------------------------------------- #
# Fakes (mirror tests/test_trajectory_eval.py)
# --------------------------------------------------------------------------- #
def _fc_event(name, args=None):
    return {
        "author": "model",
        "content": {"parts": [{"function_call": {"name": name, "args": args or {}}}]},
    }


def _fr_event(name, response=None):
    return {
        "author": "model",
        "content": {"parts": [{"function_response": {"name": name, "response": response or {}}}]},
    }


def _text_event(text):
    return {"author": "model", "content": {"parts": [{"text": text}]}}


class _FakeEngine:
    def __init__(self, events):
        self._events = events
        self.calls = []

    def stream_query(self, *, user_id, message):
        self.calls.append((user_id, message))
        yield from self._events


def _judge(text):
    """A generate_fn that ignores the prompt and returns canned judge text."""
    return lambda _prompt: text


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
class TestParseScore:
    def test_maps_1_5_to_0_1(self):
        assert parse_faithfulness_score("Score: 5") == 1.0
        assert parse_faithfulness_score("Score: 3") == pytest.approx(0.6)
        assert parse_faithfulness_score("Score: 1") == pytest.approx(0.2)

    def test_uses_last_and_handles_markdown(self):
        assert parse_faithfulness_score("Score: 2 ... final **Score:** 4") == pytest.approx(0.8)

    def test_none_when_absent(self):
        assert parse_faithfulness_score("no verdict here") is None
        assert parse_faithfulness_score("") is None
        assert parse_faithfulness_score(None) is None


class TestParseHallucinated:
    def test_parses_comma_list(self):
        assert parse_hallucinated_actions("Hallucinated: book_flight, submit_expense") == [
            "book_flight",
            "submit_expense",
        ]

    def test_handles_markdown_marker(self):
        assert parse_hallucinated_actions("**Hallucinated:** book_flight") == ["book_flight"]

    def test_none_and_absent(self):
        assert parse_hallucinated_actions("Hallucinated: NONE") == []
        assert parse_hallucinated_actions("Score: 5") == []
        assert parse_hallucinated_actions(None) == []


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #
class TestCaptureInteraction:
    def test_one_pass_text_and_trajectory(self):
        engine = _FakeEngine(
            [
                _fc_event("search_flights", {"origin": "SFO"}),
                _fr_event("search_flights", {"flights": ["FL001"]}),
                _text_event("Here are your flights."),
            ]
        )
        out = capture_interaction(engine, "Find flights from SFO", user_id="u1")
        assert out["prompt"] == "Find flights from SFO"
        assert out["response"] == "Here are your flights."
        assert [c["tool_name"] for c in out["actual_trajectory"]] == ["search_flights"]
        assert out["actual_trajectory"][0]["returned"] is True
        assert engine.calls == [("u1", "Find flights from SFO")]  # exactly one stream pass

    def test_empty_stream(self):
        out = capture_interaction(_FakeEngine([]), "hi")
        assert out["response"] == ""
        assert out["actual_trajectory"] == []


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #
class TestPrompt:
    def test_format_actual_tools_renders_and_none(self):
        assert _format_actual_tools([]) == "NONE"
        rendered = _format_actual_tools(
            [{"tool_name": "book_flight", "tool_input": {"flight_id": "FL001"}, "returned": True}]
        )
        assert "book_flight(flight_id=FL001)" in rendered
        assert "[returned]" in rendered

    def test_prompt_contains_io_tools_and_directives(self):
        prompt = build_faithfulness_prompt(
            "Book FL001",
            "I booked FL001 for you.",
            [{"tool_name": "search_flights", "tool_input": {}, "returned": True}],
        )
        assert "Book FL001" in prompt
        assert "I booked FL001 for you." in prompt
        assert "search_flights" in prompt
        assert "Hallucinated:" in prompt
        assert "Score: <1-5>" in prompt


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
class TestScoreCases:
    def test_flags_hallucinated_low_score(self):
        cases = [
            {
                "prompt": "Book FL001",
                "response": "I booked flight FL001.",
                "actual_trajectory": [{"tool_name": "search_flights", "tool_input": {}}],
            }
        ]
        result = score_cases(cases, _judge("Hallucinated: book_flight\nScore: 2"))
        assert result["score"] == pytest.approx(0.4)
        assert result["n_scored"] == 1
        assert result["flagged"] == [
            {"prompt": "Book FL001", "hallucinated": ["book_flight"], "score": pytest.approx(0.4)}
        ]

    def test_clean_high_score_no_flags(self):
        cases = [
            {
                "prompt": "Book FL001",
                "response": "I booked flight FL001.",
                "actual_trajectory": [{"tool_name": "book_flight", "tool_input": {}}],
            }
        ]
        result = score_cases(cases, _judge("Hallucinated: NONE\nScore: 5"))
        assert result["score"] == 1.0
        assert result["flagged"] == []

    def test_averages_and_skips_unparseable(self):
        cases = [{"prompt": f"p{i}", "response": "", "actual_trajectory": []} for i in range(3)]
        replies = iter(["Score: 5", "no verdict", "Score: 3"])
        result = score_cases(cases, lambda _p: next(replies))
        assert result["n_scored"] == 2
        assert result["n_total"] == 3
        assert result["score"] == pytest.approx((1.0 + 0.6) / 2)

    def test_empty_returns_none(self):
        result = score_cases([], _judge("Score: 5"))
        assert result["score"] is None
        assert result["n_total"] == 0
        assert result["flagged"] == []


class TestSelectCases:
    def test_drops_none_expected(self):
        cases = [
            {"prompt": "a", "expected_tool": "search_mcp_search_flights"},
            {"prompt": "b", "expected_tool": "none"},
            {"prompt": "c"},  # missing
        ]
        assert [c["prompt"] for c in select_faithfulness_cases(cases)] == ["a"]


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
class TestRunner:
    def test_with_fakes(self):
        engine = _FakeEngine(
            [_fc_event("book_flight", {"flight_id": "FL001"}), _text_event("Booked FL001.")]
        )
        result = run_tool_faithfulness_eval(
            engine=engine,
            cases=[{"prompt": "Book FL001", "expected_tool": "booking_mcp_book_flight"}],
            generate_fn=_judge("Hallucinated: NONE\nScore: 5"),
            warm=False,
        )
        assert result["score"] == 1.0
        assert result["n_scored"] == 1
        assert result["flagged"] == []


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
class TestMain:
    def _write_io(self, tmp_path, cases):
        path = tmp_path / "io.json"
        path.write_text(json.dumps(cases))
        return str(path)

    def test_from_json_dry_run_exit_zero(self, tmp_path, monkeypatch, capsys):
        import src.eval.tool_faithfulness as tf

        monkeypatch.setattr(tf, "build_judge_generate_fn", lambda *a, **k: _judge("Score: 5"))
        io_path = self._write_io(
            tmp_path,
            [{"prompt": "Book FL001", "response": "booked", "actual_trajectory": []}],
        )
        rc = main(["--from-json", io_path, "--dry-run"])
        assert rc == 0
        assert "tool_faithfulness" in capsys.readouterr().out

    def test_exit_nonzero_below_threshold(self, tmp_path, monkeypatch):
        import src.eval.tool_faithfulness as tf

        monkeypatch.setattr(tf, "build_judge_generate_fn", lambda *a, **k: _judge("Score: 2"))
        io_path = self._write_io(
            tmp_path,
            [{"prompt": "Book FL001", "response": "I booked FL001", "actual_trajectory": []}],
        )
        rc = main(["--from-json", io_path, "--threshold", "3.0"])
        assert rc == 1
