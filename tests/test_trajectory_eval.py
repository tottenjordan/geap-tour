"""Offline tests for deterministic trajectory / tool-use eval (no live GCP).

``extract_trajectory`` is a pure event-dict parser. ``run_trajectory_eval`` is
exercised with an injected fake ``EvalTask`` + fake engine so the wiring
(dataset shape, reference filtering, summary extraction) is verified without any
network or credentials.
"""

import pandas as pd

from src.eval.trajectory_eval import (
    CoordinatorRunnable,
    extract_trajectory,
    run_trajectory_eval,
)


def _fc_event(name, args=None, author="model"):
    """A stream_query event dict carrying a single function_call part."""
    return {
        "author": author,
        "content": {"parts": [{"function_call": {"name": name, "args": args or {}}}]},
    }


def _text_event(text):
    return {"author": "model", "content": {"parts": [{"text": text}]}}


class TestExtractTrajectory:
    def test_orders_tool_calls_and_preserves_args(self):
        events = [
            _fc_event("search_flights", {"origin": "SFO", "destination": "JFK"}),
            _fc_event("book_flight", {"flight_id": "FL001"}),
            _text_event("Booked FL001."),
        ]
        traj = extract_trajectory(events)
        assert [t["tool_name"] for t in traj] == ["search_flights", "book_flight"]
        assert traj[0]["tool_input"] == {"origin": "SFO", "destination": "JFK"}

    def test_filters_transfer_to_agent_by_default(self):
        events = [
            _fc_event("transfer_to_agent", {"agent_name": "travel_agent"}),
            _fc_event("search_hotels", {"city": "Miami"}),
        ]
        assert [t["tool_name"] for t in extract_trajectory(events)] == ["search_hotels"]

    def test_include_transfers_flag_keeps_delegation(self):
        events = [
            _fc_event("transfer_to_agent", {"agent_name": "travel_agent"}),
            _fc_event("search_hotels", {"city": "Miami"}),
        ]
        names = [t["tool_name"] for t in extract_trajectory(events, include_transfers=True)]
        assert names == ["transfer_to_agent", "search_hotels"]

    def test_empty_when_no_tool_calls(self):
        assert extract_trajectory([_text_event("hello")]) == []
        assert extract_trajectory([]) == []
        assert extract_trajectory(None) == []


class _FakeEngine:
    def __init__(self, events):
        self._events = events
        self.calls = []

    def stream_query(self, *, user_id, message):
        self.calls.append((user_id, message))
        yield from self._events


class TestCoordinatorRunnable:
    def test_query_maps_events_to_response_and_trajectory(self):
        engine = _FakeEngine(
            [
                _fc_event("search_flights", {"origin": "SFO"}),
                _text_event("Here are your flights."),
            ]
        )
        runnable = CoordinatorRunnable(engine, user_id="u1")
        out = runnable.query(input="Find flights from SFO")

        assert out["response"] == "Here are your flights."
        assert [t["tool_name"] for t in out["predicted_trajectory"]] == ["search_flights"]
        assert engine.calls == [("u1", "Find flights from SFO")]


class _FakeResult:
    def __init__(self, summary_metrics):
        self.summary_metrics = summary_metrics
        self.metrics_table = None


class _FakeEvalTask:
    """Records constructor args + returns a canned result from evaluate()."""

    last = None

    def __init__(self, *, dataset, metrics, **kwargs):
        self.dataset = dataset
        self.metrics = metrics
        self.kwargs = kwargs
        self.evaluated_with = None
        _FakeEvalTask.last = self

    def evaluate(self, *, runnable=None, **kwargs):
        self.evaluated_with = runnable
        return _FakeResult({"trajectory_exact_match/mean": 0.75})


def _cases():
    return [
        {"prompt": "Find flights from SFO to JFK", "reference_trajectory": ["search_flights"]},
        {
            "prompt": "Book FL001 then find a hotel",
            "reference_trajectory": ["book_flight", "search_hotels"],
        },
        {"prompt": "What can you help with?"},  # no reference_trajectory -> skipped
    ]


class TestRunTrajectoryEval:
    def test_scores_only_cases_with_reference_trajectory(self):
        _FakeEvalTask.last = None
        result = run_trajectory_eval(
            engine=_FakeEngine([]),
            cases=_cases(),
            eval_task_cls=_FakeEvalTask,
        )

        assert result["scored_cases"] == 2
        dataset = _FakeEvalTask.last.dataset
        assert isinstance(dataset, pd.DataFrame)
        assert len(dataset) == 2
        # reference_trajectory is materialized as {tool_name, tool_input} dicts.
        first = dataset["reference_trajectory"].iloc[0]
        assert first == [{"tool_name": "search_flights", "tool_input": {}}]

    def test_returns_summary_metrics(self):
        result = run_trajectory_eval(
            engine=_FakeEngine([]),
            cases=_cases(),
            eval_task_cls=_FakeEvalTask,
        )
        assert result["metrics"]["trajectory_exact_match/mean"] == 0.75

    def test_no_reference_cases_is_clean_noop(self):
        _FakeEvalTask.last = None
        result = run_trajectory_eval(
            engine=_FakeEngine([]),
            cases=[{"prompt": "hi"}],
            eval_task_cls=_FakeEvalTask,
        )
        assert result["scored_cases"] == 0
        assert result["metrics"] == {}
        assert _FakeEvalTask.last is None  # never constructed the task
