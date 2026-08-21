"""Offline tests for the standalone tool_use LLM-judge scorer.

The coordinator's ``tool_use_quality`` was a confirmed false-negative: the batch
eval wired the generic, delegation-blind ``types.RubricMetric.TOOL_USE_QUALITY``,
which penalizes the coordinator for delegating via ``transfer_to_agent``. This
scorer instead applies the delegation-aware ``TOOL_USE_METRIC`` (``geap_tool_use``)
through a standalone judge — the same pattern ``policy_judge`` uses to sidestep the
SDK's broken custom-pointwise-metric path. All model calls are faked; no live GCP.
"""

import pandas as pd

from src.eval import tool_use_judge as tj


def test_parse_tool_use_score_maps_1_5_to_0_1():
    assert tj.parse_tool_use_score("blah\nScore: 5") == 1.0
    assert tj.parse_tool_use_score("Score: 3") == 0.6
    assert tj.parse_tool_use_score("Score: 1") == 0.2


def test_parse_tool_use_score_uses_last_and_handles_markdown():
    # An initial mention then the final verdict — the last score wins.
    assert tj.parse_tool_use_score("Score: 2 ... final **Score:** 4") == 0.8


def test_parse_tool_use_score_none_when_absent():
    assert tj.parse_tool_use_score("no verdict here") is None
    assert tj.parse_tool_use_score("") is None
    assert tj.parse_tool_use_score(None) is None


def test_build_tool_use_prompt_contains_rubric_and_io():
    p = tj.build_tool_use_prompt("Find flights SFO to JFK", "United FL001 is $450.")
    # The GEAP rubric must be present, not the generic one. It used to be pinned by
    # asserting "transfer_to_agent" — that rubric described a delegation topology
    # deleted on 2026-08-20, so the marker is now the direct-tools premise.
    assert "SINGLE direct-tools agent" in p
    assert "transfer_to_agent" not in p
    assert "Find flights SFO to JFK" in p
    assert "United FL001 is $450." in p
    assert "Score:" in p


def test_select_tool_use_cases_keeps_tool_expected_drops_none():
    cases = [
        {"category": "travel_search", "expected_tool": "search_flights"},
        {"category": "multi_step", "expected_tool": "multiple"},
        {"category": "adversarial", "expected_tool": "none"},
        {"category": "chit_chat"},  # no expected_tool key -> dropped
    ]
    sel = tj.select_tool_use_cases(cases)
    assert {c["expected_tool"] for c in sel} == {"search_flights", "multiple"}


def test_score_pairs_averages_and_skips_unparseable():
    pairs = [("p1", "r1"), ("p2", "r2"), ("p3", "r3")]
    outs = iter(["Score: 5", "garbage — no verdict", "Score: 3"])
    res = tj.score_pairs(pairs, lambda prompt: next(outs))
    assert res["n_total"] == 3
    assert res["n_scored"] == 2
    assert res["score"] == 0.8  # (1.0 + 0.6) / 2


def test_score_pairs_empty_returns_none_score():
    res = tj.score_pairs([], lambda prompt: "Score: 5")
    assert res["score"] is None
    assert res["n_total"] == 0


def test_run_tool_use_eval_with_fakes():
    class FakeInf:
        def __init__(self, df):
            self.eval_dataset_df = df

    class FakeEvals:
        def run_inference(self, agent=None, src=None):
            return FakeInf(
                pd.DataFrame(
                    [
                        {
                            "prompt": "Find flights SFO to JFK on June 15",
                            "response": "United FL001 departs SFO for JFK at $450.",
                        },
                        # An empty/error run must be dropped, not judged.
                        {
                            "prompt": "Search hotels in NYC",
                            "response": '{"error": "Failed to parse agent run response"}',
                        },
                    ]
                )
            )

    class FakeClient:
        evals = FakeEvals()

    res = tj.run_tool_use_eval(
        "projects/x/locations/us-central1/reasoningEngines/1",
        cases=[
            {
                "prompt": "Find flights SFO to JFK on June 15",
                "category": "travel_search",
                "expected_tool": "search_flights",
                "expected_signals": [],
                "description": "d",
            }
        ],
        client=FakeClient(),
        generate_fn=lambda prompt: "Score: 4",
        warm=False,
    )
    assert res["score"] == 0.8
    assert res["n_scored"] == 1
    assert res["n_total"] == 1  # the error row was filtered before judging
