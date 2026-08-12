"""summarize_gepa_result extracts scores/lift from the GEPA result shape.

Guards against the field-name drift that produced all-None optimize metadata:
AgentWithScores exposes ``overall_score`` (not ``scores``/``score``), and the
GEPA result dict carries ``val_aggregate_scores``/``best_idx`` (not
``num_generations``/``population_size``).
"""

from types import SimpleNamespace

from src.optimize.run_optimize import summarize_gepa_result


def _fake(best_idx, val_scores, instructions, overall=None):
    agents = [
        SimpleNamespace(
            optimized_agent=SimpleNamespace(instruction=text),
            overall_score=(overall[i] if overall else None),
        )
        for i, text in enumerate(instructions)
    ]
    gepa = {
        "best_idx": best_idx,
        "val_aggregate_scores": val_scores,
        "candidates": [{}] * len(instructions),
        "total_metric_calls": 42,
        "num_full_val_evals": 3,
    }
    return SimpleNamespace(optimized_agents=agents, gepa_result=gepa)


def test_lift_and_scores_from_val_aggregate():
    s = summarize_gepa_result(_fake(1, [0.30, 0.42], ["seed", "better"]))
    assert s["best_idx"] == 1
    assert s["optimized_instruction"] == "better"
    assert s["baseline_score"] == 0.30
    assert s["best_score"] == 0.42
    assert abs(s["lift"] - 0.12) < 1e-9
    assert s["num_candidates"] == 2
    assert s["total_metric_calls"] == 42
    assert s["num_full_val_evals"] == 3


def test_best_is_seed_means_zero_lift():
    # best_idx 0 = GEPA kept the seed; the optimized prompt IS the baseline.
    s = summarize_gepa_result(_fake(0, [0.42, 0.30], ["seed", "worse"]))
    assert s["best_idx"] == 0
    assert s["optimized_instruction"] == "seed"
    assert s["lift"] == 0.0


def test_overall_score_preferred_over_val_scores():
    s = summarize_gepa_result(
        _fake(1, [0.30, 0.42], ["seed", "better"], overall={0: 0.31, 1: 0.99})
    )
    assert s["best_score"] == 0.99  # AgentWithScores.overall_score wins
    assert abs(s["lift"] - (0.99 - 0.30)) < 1e-9


def test_empty_result_does_not_crash():
    s = summarize_gepa_result(SimpleNamespace(optimized_agents=[], gepa_result={}))
    assert s["best_score"] is None
    assert s["baseline_score"] is None
    assert s["lift"] is None
    assert s["optimized_instruction"] is None
    assert s["num_candidates"] == 0
