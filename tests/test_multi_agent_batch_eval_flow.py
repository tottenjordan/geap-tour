"""The batch eval's orchestration — the part that had no tests.

`multi_agent_batch_eval` is what the CI eval gate runs and what feeds
`agent_eval/*`, and its helpers were well covered while the flow around them was
not: engine resolution, per-agent isolation, the PASSED/FAILED verdict, and the
exit code. A break in any of those degrades the gate *quietly*, which is the whole
reason the gate exists.

Each class below targets a failure this module has actually had, or one whose
symptom would be a plausible-looking number rather than an error.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import pandas as pd
import pytest

import src.eval.multi_agent_batch_eval as mabe


# --------------------------------------------------------------------------- #
# Fakes: the smallest surface `_run_single_agent_eval` actually calls.
# --------------------------------------------------------------------------- #
class _FakeEvals:
    def __init__(self, df, metrics, state="EvaluationRunState.SUCCEEDED", total_items=20):
        self._df = df
        self._metrics = metrics
        self._state = state
        self._total_items = total_items
        self.created_kwargs: dict = {}

    def run_inference(self, agent=None, src=None):
        return SimpleNamespace(eval_dataset_df=self._df, candidate_name="runtime_0")

    def create_evaluation_run(self, **kwargs):
        self.created_kwargs = kwargs
        return SimpleNamespace(name="projects/p/locations/l/evaluationRuns/1")

    def get_evaluation_run(self, name=None, include_evaluation_items=False):
        return SimpleNamespace(
            name=name,
            state=self._state,
            evaluation_run_results=SimpleNamespace(
                summary_metrics=SimpleNamespace(
                    metrics=self._metrics, total_items=self._total_items
                )
            ),
            evaluation_items=[],
        )


class _FakeClient:
    def __init__(self, evals):
        self.evals = evals


def _df(responses, *, with_tool=True):
    """An inference frame shaped like the real one."""
    events = (
        {
            "turns": [
                {
                    "events": [
                        {
                            "content": {
                                "parts": [{"function_call": {"name": "search_mcp_search_flights"}}]
                            }
                        }
                    ]
                }
            ]
        }
        if with_tool
        else {"turns": []}
    )
    return pd.DataFrame({"response": responses, "agent_data": [events] * len(responses)})


@pytest.fixture
def offline(monkeypatch):
    """Neutralize every network call the flow makes."""
    monkeypatch.setattr(mabe, "ensure_eval_experiment", lambda **_k: None)
    # `raising=False`: `vertexai.agent_engines` is a submodule, not bound on the
    # package until something imports it. The module under test survives that in
    # production only because the warmup call sits inside a try/except.
    monkeypatch.setattr(
        mabe.vertexai, "agent_engines", SimpleNamespace(get=lambda _n: object()), raising=False
    )
    monkeypatch.setattr(mabe, "warm_agent_engine", lambda _e: 1)
    return monkeypatch


# --------------------------------------------------------------------------- #
class TestEngineResolutionIsPerAgent:
    """The router is its own deployment, and getting this wrong is SILENT.

    Before per-agent resolution, one engine served the whole run, so a bare
    invocation scored the 40 ROUTER_EVAL_CASES against a *coordinator* and reported
    it as router quality. Nothing errored — the numbers were simply about a
    different agent.
    """

    def test_the_router_resolves_to_the_router_engine(self):
        assert mabe.ROUTER_ENGINE_ID in mabe._engine_for_agent("router_agent", None)

    @pytest.mark.parametrize("agent", ["coordinator_agent", "travel_agent", "expense_agent"])
    def test_everything_else_resolves_to_the_default_engine(self, agent):
        assert mabe.AGENT_ENGINE_ID in mabe._engine_for_agent(agent, None)

    def test_the_two_are_actually_different(self):
        """If the ids ever coincide the tests above pass vacuously."""
        if mabe.ROUTER_ENGINE_ID == mabe.AGENT_ENGINE_ID:
            pytest.skip("router and coordinator share an engine id in this env")
        assert mabe._engine_for_agent("router_agent", None) != mabe._engine_for_agent(
            "coordinator_agent", None
        )

    def test_an_explicit_agent_id_pins_every_agent(self):
        """The bake-off contract: one engine for the whole run, router included."""
        pinned = [mabe._engine_for_agent(a, "999") for a in mabe.ALL_AGENTS]
        assert all(e.endswith("/999") for e in pinned)
        assert len(set(pinned)) == 1

    def test_an_unknown_agent_falls_back_rather_than_crashing(self):
        assert mabe.AGENT_ENGINE_ID in mabe._engine_for_agent("not_an_agent", None)


class TestTheOrchestratorIsolatesAgents:
    @pytest.fixture(autouse=True)
    def _no_network(self, monkeypatch):
        monkeypatch.setattr(mabe.vertexai, "init", lambda **_k: None)
        monkeypatch.setattr(mabe, "Client", lambda **_k: object())

    def test_one_agent_failing_does_not_abort_the_rest(self, monkeypatch):
        """A single dead engine must not cost the whole run's results."""

        def flaky(client, agent_name, agent_resource_name, score_threshold, limit=None):
            if agent_name == "travel_agent":
                raise RuntimeError("engine exploded")
            return {"agent": agent_name, "status": "PASSED", "test_cases": 3}

        monkeypatch.setattr(mabe, "_run_single_agent_eval", flaky)
        out = mabe.run_multi_agent_batch_eval(agents=["coordinator_agent", "travel_agent"])

        assert out["agents"]["coordinator_agent"]["status"] == "PASSED"
        assert out["agents"]["travel_agent"]["status"] == "ERROR"
        assert "engine exploded" in out["agents"]["travel_agent"]["error"]

    def test_an_errored_agent_makes_the_run_not_all_passed(self, monkeypatch):
        """ERROR must not be quietly excluded from the verdict."""

        def flaky(client, agent_name, agent_resource_name, score_threshold, limit=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(mabe, "_run_single_agent_eval", flaky)
        out = mabe.run_multi_agent_batch_eval(agents=["coordinator_agent"])
        assert out["all_passed"] is False
        assert out["agents_passed"] == 0

    def test_the_results_record_every_engine_used(self, monkeypatch):
        """A run spanning deployments must say so; `agent_engine` alone cannot."""
        monkeypatch.setattr(
            mabe,
            "_run_single_agent_eval",
            lambda **kw: {"agent": kw["agent_name"], "status": "PASSED", "test_cases": 1},
        )
        out = mabe.run_multi_agent_batch_eval(agents=["coordinator_agent", "router_agent"])

        assert set(out["agent_engines"]) == {"coordinator_agent", "router_agent"}
        assert mabe.ROUTER_ENGINE_ID in out["agent_engines"]["router_agent"]
        assert out["agent_engine"], "back-compat single-engine key must stay populated"

    def test_the_limit_reaches_each_agent(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            mabe,
            "_run_single_agent_eval",
            lambda **kw: (
                seen.append(kw["limit"])
                or {"agent": kw["agent_name"], "status": "PASSED", "test_cases": 1}
            ),
        )
        mabe.run_multi_agent_batch_eval(agents=["coordinator_agent"], limit=8)
        assert seen == [8]


class TestTheVerdictAndTheMetricShape:
    """What `_run_single_agent_eval` returns is what everything downstream reads."""

    METRICS: ClassVar[dict] = {
        "runtime_0/safety_v1/AVERAGE": 0.9,
        "runtime_0/hallucination_v1/AVERAGE": 0.8,
    }

    def _run(self, offline, metrics, responses=("an answer", "another"), threshold=3.0, **kw):
        evals = _FakeEvals(_df(list(responses)), metrics, **kw)
        return (
            mabe._run_single_agent_eval(
                client=_FakeClient(evals),
                agent_name="coordinator_agent",
                agent_resource_name="projects/p/locations/l/reasoningEngines/1",
                score_threshold=threshold,
            ),
            evals,
        )

    def test_scores_above_the_floor_pass(self, offline):
        result, _ = self._run(offline, self.METRICS)
        assert result["status"] == "PASSED"

    def test_a_single_metric_below_the_floor_fails_the_run(self, offline):
        result, _ = self._run(offline, {**self.METRICS, "runtime_0/safety_v1/AVERAGE": 0.1})
        assert result["status"] == "FAILED"

    def test_the_threshold_is_normalized_from_one_to_five(self, offline):
        """Scores arrive 0-1; the CLI takes 1-5. A missing /5 would compare 0.9 to
        3.0 and fail every run."""
        result, _ = self._run(offline, self.METRICS, threshold=3.0)
        detail = next(iter(result["metrics"].values()))
        assert detail["threshold"] == pytest.approx(0.6)

    def test_each_metric_carries_score_threshold_and_passed(self, offline):
        """The shape downstream reads. `publish_router_quality` assumed a bare float
        here, passed its unit tests, and raised TypeError on the first real call."""
        result, _ = self._run(offline, self.METRICS)
        detail = result["metrics"]["runtime_0/safety_v1"]
        assert set(detail) >= {"score", "threshold", "passed", "low_confidence"}
        assert detail["score"] == pytest.approx(0.9)

    def test_only_average_keys_become_metrics(self, offline):
        """Per-item keys would double-count into the same name."""
        result, _ = self._run(offline, {**self.METRICS, "runtime_0/safety_v1/STDDEV": 0.05})
        assert "runtime_0/safety_v1" in result["metrics"]
        assert all("STDDEV" not in k for k in result["metrics"])

    def test_a_small_run_is_flagged_low_confidence(self, offline):
        result, _ = self._run(offline, self.METRICS, total_items=2)
        assert all(d["low_confidence"] for d in result["metrics"].values())

    def test_no_metrics_is_not_a_pass(self, offline):
        """An eval that returned nothing must not read as success — the defect
        `simulated_eval` had to be fixed for."""
        result, _ = self._run(offline, {})
        assert result["metrics"] == {}
        assert result["status"] != "PASSED"


class TestEmptyResponsesAreInfraNotQuality:
    def test_an_all_empty_run_is_skipped_not_scored(self, offline):
        """Every rubric would grade the empty string and return a low mean, which
        reads as a quality regression instead of a dead engine."""
        evals = _FakeEvals(_df(["", ""]), {"runtime_0/safety_v1/AVERAGE": 0.9})
        result = mabe._run_single_agent_eval(
            client=_FakeClient(evals),
            agent_name="coordinator_agent",
            agent_resource_name="projects/p/locations/l/reasoningEngines/1",
            score_threshold=3.0,
        )
        assert result["status"] == "SKIPPED"
        assert result["empty_rate"] == 1.0
        assert "infra" in result["reason"]
        assert result["metrics"] == {}

    def test_a_partially_empty_run_still_scores_and_reports_the_rate(self, offline):
        evals = _FakeEvals(_df(["a real answer", ""]), {"runtime_0/safety_v1/AVERAGE": 0.9})
        result = mabe._run_single_agent_eval(
            client=_FakeClient(evals),
            agent_name="coordinator_agent",
            agent_resource_name="projects/p/locations/l/reasoningEngines/1",
            score_threshold=3.0,
        )
        assert result["status"] == "PASSED"
        assert result["empty_responses"] == 1
        assert result["empty_rate"] == 0.5

    def test_tool_use_is_dropped_when_nothing_called_a_tool(self, offline):
        """The service rejects the metric outright, and the run would come back one
        metric short with no explanation."""
        evals = _FakeEvals(_df(["answer"], with_tool=False), {"runtime_0/safety_v1/AVERAGE": 0.9})
        mabe._run_single_agent_eval(
            client=_FakeClient(evals),
            agent_name="coordinator_agent",
            agent_resource_name="projects/p/locations/l/reasoningEngines/1",
            score_threshold=3.0,
        )
        names = [getattr(m, "name", str(m)) for m in evals.created_kwargs["metrics"]]
        assert "TOOL_USE_QUALITY" not in names


class TestTheCliFailsLoudly:
    def test_a_failing_run_exits_non_zero(self, monkeypatch):
        """An advisory gate that always exits 0 is a green tick with no content."""
        monkeypatch.setattr(mabe, "run_multi_agent_batch_eval", lambda **_k: {"all_passed": False})
        monkeypatch.setattr("sys.argv", ["prog"])
        with pytest.raises(SystemExit) as exc:
            mabe.main()
        assert exc.value.code == 1

    def test_a_passing_run_exits_zero(self, monkeypatch):
        monkeypatch.setattr(mabe, "run_multi_agent_batch_eval", lambda **_k: {"all_passed": True})
        monkeypatch.setattr("sys.argv", ["prog"])
        mabe.main()  # must not raise

    def test_list_cases_contacts_nothing(self, monkeypatch):
        called = []
        monkeypatch.setattr(mabe, "list_all_cases", lambda: called.append(True))
        monkeypatch.setattr(
            mabe,
            "run_multi_agent_batch_eval",
            lambda **_k: pytest.fail("--list-cases must not run an eval"),
        )
        monkeypatch.setattr("sys.argv", ["prog", "--list-cases"])
        mabe.main()
        assert called == [True]

    def test_the_cli_threads_its_arguments_through(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            mabe,
            "run_multi_agent_batch_eval",
            lambda **kw: seen.update(kw) or {"all_passed": True},
        )
        monkeypatch.setattr(
            "sys.argv",
            [
                "prog",
                "--agents",
                "router_agent",
                "--agent-id",
                "42",
                "--limit",
                "8",
                "--threshold",
                "4.0",
            ],
        )
        mabe.main()
        assert seen["agents"] == ["router_agent"]
        assert seen["agent_id"] == "42"
        assert seen["limit"] == 8
        assert seen["score_threshold"] == 4.0


class TestDefensivePathsDoNotCrashOrLie:
    """The branches that only run when something upstream is already wrong.

    Not added for the coverage number: each one is a place where a crash, or a
    wrong verdict, would be attributed to the agent rather than to the harness.
    """

    def test_a_failed_eval_run_reports_failed_with_the_error(self, offline):
        """Distinct from 'scores were low' — this is the service refusing to grade.
        It must carry the error so the run is diagnosable, and must NOT be a pass."""
        evals = _FakeEvals(_df(["answer"]), {}, state="EvaluationRunState.FAILED")
        result = mabe._run_single_agent_eval(
            client=_FakeClient(evals),
            agent_name="coordinator_agent",
            agent_resource_name="projects/p/locations/l/reasoningEngines/1",
            score_threshold=3.0,
        )
        assert result["status"] == "FAILED"
        assert "error" in result
        assert "metrics" not in result, "a refused run has no scores to report"

    @pytest.mark.parametrize(
        "cell",
        [None, "not json", 42, {"turns": None}, {"no_turns_key": 1}, '{"turns": []}'],
        ids=["none", "bad-json", "int", "null-turns", "missing-key", "json-string"],
    )
    def test_malformed_trace_data_counts_as_tool_free(self, cell):
        """`agent_data` arrives as a dict or a JSON string depending on the parse
        path. A malformed row must count as 'no tools', never raise — a crash here
        would abort a whole run over one bad row."""
        assert mabe._agent_data_events(cell) == []

    def test_a_row_with_real_events_is_still_read(self):
        """The other half: the tolerant parser must not swallow good data."""
        events = mabe._agent_data_events(
            {"turns": [{"events": [{"content": {"parts": [{"text": "hi"}]}}]}]}
        )
        assert len(events) == 1
