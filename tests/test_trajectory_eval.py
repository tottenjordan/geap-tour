"""Offline tests for deterministic trajectory / tool-use eval (no live GCP).

``extract_trajectory`` is a pure event-dict parser. ``run_trajectory_eval`` is
exercised with an injected fake ``EvalTask`` + fake engine so the wiring
(dataset shape, reference filtering, summary extraction) is verified without any
network or credentials.
"""

import pandas as pd

from src.eval.trajectory_eval import (
    CoordinatorRunnable,
    capture_trajectory,
    extract_trajectory,
    returned_tool_names,
    run_trajectory_eval,
)


def _fc_event(name, args=None, author="model"):
    """A stream_query event dict carrying a single function_call part."""
    return {
        "author": author,
        "content": {"parts": [{"function_call": {"name": name, "args": args or {}}}]},
    }


def _fr_event(name, response=None, author="model"):
    """A stream_query event dict carrying a single function_response part."""
    return {
        "author": author,
        "content": {"parts": [{"function_response": {"name": name, "response": response or {}}}]},
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


class TestReturnedToolNames:
    def test_collects_names_from_function_response_parts(self):
        events = [
            _fc_event("search_flights", {"origin": "SFO"}),
            _fr_event("search_flights", {"flights": ["FL001"]}),
            _fr_event("book_flight", {"status": "confirmed"}),
        ]
        assert returned_tool_names(events) == {"search_flights", "book_flight"}

    def test_empty_when_no_responses(self):
        assert returned_tool_names([_fc_event("search_flights")]) == set()
        assert returned_tool_names([]) == set()
        assert returned_tool_names(None) == set()


class TestCaptureTrajectory:
    def test_records_returned_flag_per_call(self):
        events = [
            _fc_event("search_flights", {"origin": "SFO"}),
            _fr_event("search_flights", {"flights": ["FL001"]}),
            _fc_event("book_flight", {"flight_id": "FL001"}),  # called but no response
        ]
        traj = capture_trajectory(events)
        by_name = {c["tool_name"]: c["returned"] for c in traj}
        assert by_name == {"search_flights": True, "book_flight": False}

    def test_preserves_call_order_and_args(self):
        events = [
            _fc_event("search_flights", {"origin": "SFO", "destination": "JFK"}),
            _fr_event("search_flights"),
        ]
        traj = capture_trajectory(events)
        assert traj[0]["tool_name"] == "search_flights"
        assert traj[0]["tool_input"] == {"origin": "SFO", "destination": "JFK"}

    def test_filters_transfer_to_agent_by_default(self):
        events = [
            _fc_event("transfer_to_agent", {"agent_name": "travel_agent"}),
            _fc_event("search_hotels", {"city": "Miami"}),
        ]
        assert [c["tool_name"] for c in capture_trajectory(events)] == ["search_hotels"]

    def test_include_transfers_keeps_delegation(self):
        events = [
            _fc_event("transfer_to_agent", {"agent_name": "travel_agent"}),
            _fc_event("search_hotels", {"city": "Miami"}),
        ]
        names = [c["tool_name"] for c in capture_trajectory(events, include_transfers=True)]
        assert names == ["transfer_to_agent", "search_hotels"]

    def test_empty_input(self):
        assert capture_trajectory([]) == []
        assert capture_trajectory(None) == []


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


class _FlakyEngine:
    """Yields nothing on the first N calls, then real events."""

    def __init__(self, events, *, empty_first: int):
        self._events = events
        self._empty_first = empty_first
        self.calls = 0

    def stream_query(self, *, user_id, message):
        self.calls += 1
        return iter([] if self.calls <= self._empty_first else self._events)


class TestRunnableRobustness:
    """EvalTask fans out concurrently, which is the documented empty-at-200 trigger,
    and an empty trajectory nan-poisons the whole run. See the class docstring."""

    def test_retries_an_empty_turn(self):
        engine = _FlakyEngine([_fc_event("search_flights", {"origin": "SFO"})], empty_first=1)
        out = CoordinatorRunnable(engine, empty_retries=2).query(input="Find flights")
        assert [t["tool_name"] for t in out["predicted_trajectory"]] == ["search_flights"]
        assert engine.calls == 2

    def test_gives_up_after_the_retry_budget(self):
        engine = _FlakyEngine([], empty_first=99)
        out = CoordinatorRunnable(engine, empty_retries=3).query(input="Find flights")
        assert out["predicted_trajectory"] == []
        assert engine.calls == 3

    def test_a_good_turn_is_not_retried(self):
        engine = _FlakyEngine([_fc_event("search_flights", {})], empty_first=0)
        CoordinatorRunnable(engine, empty_retries=3).query(input="Find flights")
        assert engine.calls == 1

    def test_predicted_names_are_normalized(self):
        """References are bare; the runtime emits registry-prefixed names."""
        engine = _FakeEngine([_fc_event("booking_mcp_book_flight", {"flight_id": "FL001"})])
        out = CoordinatorRunnable(engine).query(input="Book FL001")
        assert [t["tool_name"] for t in out["predicted_trajectory"]] == ["book_flight"]


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


class _StubRunnable:
    """Returns a canned predicted trajectory per prompt (``None`` => empty turn)."""

    def __init__(self, by_prompt):
        self._by_prompt = by_prompt

    def query(self, input: str = "", **kwargs) -> dict:
        names = self._by_prompt.get(input) or []
        return {
            "response": "ok" if names else "",
            "predicted_trajectory": [{"tool_name": n, "tool_input": {}} for n in names],
        }


class TestRunTrajectoryEval:
    def test_scores_only_cases_with_reference_trajectory(self):
        _FakeEvalTask.last = None
        result = run_trajectory_eval(
            engine=_FakeEngine([]),
            cases=_cases(),
            eval_task_cls=_FakeEvalTask,
            runnable=_StubRunnable(
                {
                    "Find flights from SFO to JFK": ["search_flights"],
                    "Book FL001 then find a hotel": ["book_flight", "search_hotels"],
                }
            ),
        )

        assert result["scored_cases"] == 2
        assert result["empty_trajectories"] == 0
        dataset = _FakeEvalTask.last.dataset
        assert isinstance(dataset, pd.DataFrame)
        assert len(dataset) == 2
        # reference_trajectory is materialized as {tool_name, tool_input} dicts.
        first = dataset["reference_trajectory"].iloc[0]
        assert first == [{"tool_name": "search_flights", "tool_input": {}}]

    def test_predictions_are_supplied_in_the_dataset_not_via_runnable(self):
        """BYOR mode: handing EvalTask the runnable fans out concurrently and an
        empty trajectory then nan-poisons every metric. See the module docstring."""
        _FakeEvalTask.last = None
        run_trajectory_eval(
            engine=_FakeEngine([]),
            cases=_cases(),
            eval_task_cls=_FakeEvalTask,
            runnable=_StubRunnable({"Find flights from SFO to JFK": ["search_flights"]}),
        )
        task = _FakeEvalTask.last
        assert "predicted_trajectory" in task.dataset.columns
        assert task.evaluated_with is None  # never delegated generation to EvalTask

    def test_empty_turns_are_partitioned_out_not_scored(self):
        """An empty predicted_trajectory is rejected by the API as 'field not set',
        so it must never reach the dataset — it is counted instead."""
        _FakeEvalTask.last = None
        result = run_trajectory_eval(
            engine=_FakeEngine([]),
            cases=_cases(),
            eval_task_cls=_FakeEvalTask,
            runnable=_StubRunnable({"Find flights from SFO to JFK": ["search_flights"]}),
        )
        assert result["scored_cases"] == 1
        assert result["empty_trajectories"] == 1
        assert len(_FakeEvalTask.last.dataset) == 1

    def test_all_empty_is_a_clean_noop(self):
        _FakeEvalTask.last = None
        result = run_trajectory_eval(
            engine=_FakeEngine([]),
            cases=_cases(),
            eval_task_cls=_FakeEvalTask,
            runnable=_StubRunnable({}),
        )
        assert result["scored_cases"] == 0
        assert result["empty_trajectories"] == 2
        assert _FakeEvalTask.last is None  # no task constructed, no API call

    def test_predicted_args_are_blanked_to_match_reference_granularity(self):
        """`reference_trajectory` is names-only, and the metrics compare the whole
        {tool_name, tool_input} dict — leaving real args on scores every row 0."""

        class _ArgsRunnable:
            def query(self, input: str = "", **kwargs) -> dict:
                return {
                    "response": "ok",
                    "predicted_trajectory": [
                        {"tool_name": "search_flights", "tool_input": {"origin": "SFO"}}
                    ],
                }

        _FakeEvalTask.last = None
        run_trajectory_eval(
            engine=_FakeEngine([]),
            cases=_cases()[:1],
            eval_task_cls=_FakeEvalTask,
            runnable=_ArgsRunnable(),
        )
        predicted = _FakeEvalTask.last.dataset["predicted_trajectory"].iloc[0]
        assert predicted == [{"tool_name": "search_flights", "tool_input": {}}]

    def test_returns_summary_metrics(self):
        result = run_trajectory_eval(
            engine=_FakeEngine([]),
            cases=_cases(),
            eval_task_cls=_FakeEvalTask,
            runnable=_StubRunnable({"Find flights from SFO to JFK": ["search_flights"]}),
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


class TestNormalizeToolName:
    """Runtime tool names carry a registry prefix; our references don't.

    ``registry.get_mcp_tools`` resolves through Agent Registry, which namespaces
    tools as ``<domain>_mcp_<tool>`` (confirmed live: ``booking_mcp_book_flight``).
    The direct-URL fallback yields bare names — and that fallback was the normal
    path until the 2026-08-15 IAM remediation, which is why the hand-authored
    references use bare names. Any literal name comparison needs both sides in one
    form. See docs/notes/trajectory-criterion.md.
    """

    def test_strips_the_registry_prefix(self):
        from src.eval.trajectory_eval import normalize_tool_name

        assert normalize_tool_name("booking_mcp_book_flight") == "book_flight"
        assert normalize_tool_name("expense_mcp_check_expense_policy") == "check_expense_policy"

    def test_bare_names_pass_through(self):
        from src.eval.trajectory_eval import normalize_tool_name

        assert normalize_tool_name("book_flight") == "book_flight"

    def test_unknown_domain_is_left_alone(self):
        """Only real server domains are stripped — a normalizer that guesses is
        worse than none."""
        from src.eval.trajectory_eval import normalize_tool_name

        assert normalize_tool_name("weather_mcp_get_forecast") == "weather_mcp_get_forecast"

    def test_real_domain_but_unknown_tool_is_left_alone(self):
        from src.eval.trajectory_eval import normalize_tool_name

        assert normalize_tool_name("booking_mcp_teleport") == "booking_mcp_teleport"

    def test_every_real_tool_round_trips(self):
        from src.eval.trajectory_eval import normalize_tool_name
        from src.eval.verify_mcp_tools import EXPECTED_TOOLS

        for domain, tools in EXPECTED_TOOLS.items():
            for tool in tools:
                assert normalize_tool_name(f"{domain}_mcp_{tool}") == tool

    def test_handles_empty_and_none(self):
        from src.eval.trajectory_eval import normalize_tool_name

        assert normalize_tool_name("") == ""
        assert normalize_tool_name(None) is None


class TestCalibrationMatchTypes:
    """The calibration spike reimplements ADK's three match types — pin them.

    Reimplemented (not imported) because ADK's evaluator only accepts full
    ``Invocation`` objects and we score names and names+args separately. These
    cases are lifted from ADK's own ``ToolTrajectoryCriterion`` docstring examples.
    """

    def test_exact_requires_a_perfect_sequence(self):
        from src.eval.calibrate_trajectory import matches

        assert matches(["a", "b"], ["a", "b"], "EXACT")
        assert not matches(["a", "b", "c"], ["a", "b"], "EXACT")
        assert not matches(["b", "a"], ["a", "b"], "EXACT")

    def test_in_order_allows_extras_but_not_reordering(self):
        from src.eval.calibrate_trajectory import matches

        # ADK docstring example 1: extras interleaved is a match.
        assert matches(["T1", "T1.1", "T2", "T2.1", "T3"], ["T1", "T2", "T3"], "IN_ORDER")
        # ADK docstring example 2: a missing expected call is not.
        assert not matches(["T1", "T2", "T3"], ["T1", "T2", "T3", "T4"], "IN_ORDER")
        assert not matches(["b", "a"], ["a", "b"], "IN_ORDER")

    def test_any_order_ignores_order_but_not_absence(self):
        from src.eval.calibrate_trajectory import matches

        assert matches(["T2", "T1", "T3"], ["T1", "T2", "T3"], "ANY_ORDER")
        assert not matches(["T1", "T2", "T3"], ["T1", "T2", "T3", "T4"], "ANY_ORDER")

    def test_any_order_counts_duplicates(self):
        """Two expected calls to the same tool need two actual ones."""
        from src.eval.calibrate_trajectory import matches

        assert not matches(["a"], ["a", "a"], "ANY_ORDER")
        assert matches(["a", "a"], ["a", "a"], "ANY_ORDER")

    def test_unknown_match_type_raises(self):
        import pytest

        from src.eval.calibrate_trajectory import matches

        with pytest.raises(ValueError):
            matches([], [], "FUZZY")

    def test_summarize_reports_raw_and_normalized(self):
        from src.eval.calibrate_trajectory import summarize

        rows = [
            {
                "actual_raw": ["expense_mcp_check_expense_policy"],
                "actual_norm": ["check_expense_policy"],
                "expected": ["check_expense_policy"],
            }
        ]
        rates = summarize(rows)
        assert rates["EXACT"]["raw"] == 0.0
        assert rates["EXACT"]["normalized"] == 1.0


class TestTrajectoryEvalIsWiredIn:
    """`run_trajectory_eval` was finished, tested, and called by nothing.

    It scored ~0 because trajectory comparison is literal and the runtime emits
    registry-prefixed names (`booking_mcp_book_flight`) while the curated
    references are bare — measured live 2026-08-21 at 0% raw vs 68% normalized.
    Now that normalization exists, it is wired into `run_all_evals`; this pins it
    so it cannot quietly become dead code again.
    """

    def test_run_all_evals_references_it(self):
        import inspect

        from src.eval import run_all_evals

        assert "run_trajectory_eval" in inspect.getsource(run_all_evals)

    def test_phase_banners_are_consistently_numbered(self):
        """Adding a phase means renumbering every banner — easy to half-do."""
        import inspect
        import re

        from src.eval import run_all_evals

        totals = set(re.findall(r"\[Phase \d+/(\d+)\]", inspect.getsource(run_all_evals)))
        assert len(totals) == 1, f"mixed phase totals: {totals}"

    def test_trajectory_runs_under_batch_only(self):
        """--batch-only is the cheap offline path; trajectory is offline too."""
        import inspect

        from src.eval import run_all_evals

        src = inspect.getsource(run_all_evals.run_all_evals)
        assert src.index("TRAJECTORY EVALUATION") < src.index("if batch_only:")


class TestReferenceTrajectoriesAreRealTools:
    """A renamed tool would silently zero the metric rather than fail loudly."""

    def test_every_reference_entry_is_a_real_tool(self):
        from src.eval.batch_eval import EVAL_CASES
        from src.eval.trajectory_eval import normalize_tool_name
        from src.eval.verify_mcp_tools import EXPECTED_TOOLS

        real = {t for tools in EXPECTED_TOOLS.values() for t in tools}
        unknown = {
            name
            for case in EVAL_CASES
            for name in (case.get("reference_trajectory") or [])
            if normalize_tool_name(name) not in real
        }
        assert not unknown, f"reference_trajectory names off the servers: {sorted(unknown)}"


class TestSamplerConfigsSpellOutMatchType:
    """`tool_trajectory_avg_score` defaults to EXACT when `match_type` is omitted.

    A bare float threshold therefore silently buys the strictest comparison — no
    extra tool calls allowed at all. If a config ever opts into the metric, it must
    say which match type it means. See docs/notes/trajectory-criterion.md.
    """

    def test_any_trajectory_criterion_declares_its_match_type(self):
        import glob
        import json

        offenders = []
        for path in sorted(glob.glob("src/optimize/*sampler_config.json")):
            with open(path) as fh:
                criteria = (json.load(fh).get("eval_config") or {}).get("criteria") or {}
            crit = criteria.get("tool_trajectory_avg_score")
            if crit is not None and not (isinstance(crit, dict) and crit.get("match_type")):
                offenders.append(path)
        assert not offenders, f"declare match_type (default is EXACT) in: {offenders}"
