"""Offline tests for the standalone policy_compliance LLM-judge scorer.

The scorer bypasses `client.evals` (whose custom-pointwise-metric path is broken
in the installed vertexai SDK — the judge scores correctly but the service's
response parser rejects the markdown verdict as invalid JSON). All model calls
are faked; no live GCP.
"""

import pandas as pd

from src.eval import policy_judge as pj


def test_parse_policy_score_maps_1_5_to_0_1():
    assert pj.parse_policy_score("blah\nScore: 5") == 1.0
    assert pj.parse_policy_score("Score: 3") == 0.6
    assert pj.parse_policy_score("Score: 1") == 0.2


def test_parse_policy_score_uses_last_and_handles_markdown():
    # An initial mention then the final verdict — the last score wins.
    assert pj.parse_policy_score("Score: 2 ... final **Score:** 4") == 0.8


def test_parse_policy_score_none_when_absent():
    assert pj.parse_policy_score("no verdict here") is None
    assert pj.parse_policy_score("") is None
    assert pj.parse_policy_score(None) is None


def test_build_policy_prompt_contains_rubric_and_io():
    p = pj.build_policy_prompt("Expense $500 dinner?", "The limit is $75.")
    assert "$75" in p  # rubric instruction carries the policy limits
    assert "Expense $500 dinner?" in p
    assert "The limit is $75." in p
    assert "Score:" in p


def test_select_policy_cases_filters_to_expense_and_routing():
    cases = [
        {"category": "expense_policy"},
        {"category": "travel_search"},
        {"category": "routing_expense"},
        {"category": "routing_travel"},
    ]
    sel = pj.select_policy_cases(cases)
    assert {c["category"] for c in sel} == {"expense_policy", "routing_expense"}


def test_score_pairs_averages_and_skips_unparseable():
    pairs = [("p1", "r1"), ("p2", "r2"), ("p3", "r3")]
    outs = iter(["Score: 5", "garbage — no verdict", "Score: 3"])
    res = pj.score_pairs(pairs, lambda prompt: next(outs))
    assert res["n_total"] == 3
    assert res["n_scored"] == 2
    assert res["score"] == 0.8  # (1.0 + 0.6) / 2


def test_score_pairs_empty_returns_none_score():
    res = pj.score_pairs([], lambda prompt: "Score: 5")
    assert res["score"] is None
    assert res["n_total"] == 0


def test_run_policy_compliance_eval_with_fakes():
    class FakeInf:
        def __init__(self, df):
            self.eval_dataset_df = df

    class FakeEvals:
        def run_inference(self, agent=None, src=None):
            return FakeInf(
                pd.DataFrame(
                    [
                        {
                            "prompt": "Expense $500 dinner (meals)?",
                            "response": "The meal limit is $75; $500 exceeds it.",
                        },
                        # An empty/error run must be dropped, not judged.
                        {
                            "prompt": "Check policy for lodging",
                            "response": '{"error": "Failed to parse agent run response"}',
                        },
                    ]
                )
            )

    class FakeClient:
        evals = FakeEvals()

    res = pj.run_policy_compliance_eval(
        "projects/x/locations/us-central1/reasoningEngines/1",
        cases=[
            {
                "prompt": "Expense $500 dinner (meals)?",
                "category": "expense_policy",
                "expected_tool": "t",
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


def test_run_policy_compliance_eval_with_panel():
    class FakeInf:
        def __init__(self, df):
            self.eval_dataset_df = df

    class FakeEvals:
        def run_inference(self, agent=None, src=None):
            return FakeInf(
                pd.DataFrame(
                    [{"prompt": "Expense $500 dinner (meals)?", "response": "Over the $75 limit."}]
                )
            )

    class FakeClient:
        evals = FakeEvals()

    # A three-model panel: two agree at 4/5, one contrarian at 2/5. The median
    # (0.8) is robust to the outlier, and reliability is reported.
    panel = [lambda _p: "Score: 4", lambda _p: "Score: 4", lambda _p: "Score: 2"]
    res = pj.run_policy_compliance_eval(
        "projects/x/locations/us-central1/reasoningEngines/1",
        cases=[
            {
                "prompt": "Expense $500 dinner (meals)?",
                "category": "expense_policy",
                "expected_tool": "t",
                "expected_signals": [],
                "description": "d",
            }
        ],
        client=FakeClient(),
        judges=panel,
        warm=False,
    )
    assert res["score"] == 0.8  # median of {0.8, 0.8, 0.4}
    assert res["n_scored"] == 1
    assert res["reliability"]["n_judges"] == 3
    assert "alpha" in res["reliability"]
