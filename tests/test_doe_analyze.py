"""Main-effect arithmetic and report rendering for src.doe.analyze."""

import numpy as np
import pandas as pd
import pytest

from src.doe import analyze as an
from src.doe.factors import Factor

# Two simple 2-level factors for hand-checked arithmetic.
A = Factor(name="A", channel="param", levels={"lo": {"p": 1}, "hi": {"p": 2}})
B = Factor(name="B", channel="param", levels={"lo": {"q": 1}, "hi": {"q": 2}})


def _table():
    # Full 2x2 with a known response y.
    return pd.DataFrame(
        [
            {"design_point": "dp1", "A": "lo", "B": "lo", "tool_use_quality": 0.10},
            {"design_point": "dp2", "A": "hi", "B": "lo", "tool_use_quality": 0.30},
            {"design_point": "dp3", "A": "lo", "B": "hi", "tool_use_quality": 0.20},
            {"design_point": "dp4", "A": "hi", "B": "hi", "tool_use_quality": 0.40},
        ]
    )


def test_main_effect_arithmetic():
    df = _table()
    # A high rows: dp2(0.30), dp4(0.40) -> mean 0.35; A low: dp1(0.10), dp3(0.20) -> 0.15
    assert an.main_effect(df, A, "tool_use_quality") == pytest.approx(0.20)
    # B high rows: dp3(0.20), dp4(0.40) -> 0.30; B low: 0.10,0.30 -> 0.20
    assert an.main_effect(df, B, "tool_use_quality") == pytest.approx(0.10)


def test_single_factor_main_effect_is_high_minus_low():
    # The bake-off is a k=1 design: two rows, gemini (low) vs claude (high).
    # main_effect must still compute claude_mean - gemini_mean with one row
    # per partition (no k>=2 assumption).
    backend = Factor(
        name="model_backend",
        channel="engine_env",
        levels={"gemini": {"COORDINATOR_MODEL": "g"}, "claude": {"COORDINATOR_MODEL": "c"}},
    )
    df = pd.DataFrame(
        [
            {"design_point": "dp01", "model_backend": "gemini", "tool_use_quality": 0.30},
            {"design_point": "dp02", "model_backend": "claude", "tool_use_quality": 0.42},
        ]
    )
    assert an.main_effect(df, backend, "tool_use_quality") == pytest.approx(0.12)
    ranked = an.rank_factors(df, [backend], "tool_use_quality")
    assert [name for name, _ in ranked] == ["model_backend"]
    rec = an.recommend_config(df, [backend])
    assert rec["model_backend"] == "claude"  # higher quality level


def test_main_effect_nan_when_level_absent():
    df = _table()
    df = df[df["A"] == "lo"]  # no high level of A present
    assert np.isnan(an.main_effect(df, A, "tool_use_quality"))


def test_rank_factors_orders_by_abs_effect():
    df = _table()
    ranked = an.rank_factors(df, [A, B], "tool_use_quality")
    assert [name for name, _ in ranked] == ["A", "B"]  # |0.20| > |0.10|


def test_main_effects_table_shape():
    df = _table()
    tbl = an.main_effects_table(df, [A, B], responses=("tool_use_quality",))
    assert list(tbl.index) == ["A", "B"]
    assert "tool_use_quality" in tbl.columns


def test_cost_quality_frontier_selects_non_dominated():
    df = pd.DataFrame(
        [
            {
                "design_point": "a",
                "savings_pct": 60.0,
                "tool_use_quality": 0.4,
                "final_response_match": 0.4,
            },
            {
                "design_point": "b",
                "savings_pct": 30.0,
                "tool_use_quality": 0.2,
                "final_response_match": 0.2,
            },  # dominated by a
            {
                "design_point": "c",
                "savings_pct": 80.0,
                "tool_use_quality": 0.1,
                "final_response_match": 0.1,
            },  # high savings, low quality
        ]
    )
    front = an.cost_quality_frontier(df)
    assert set(front["design_point"]) == {"a", "c"}  # b is dominated


def test_recommend_config_picks_higher_quality_level():
    df = _table().rename(columns={"tool_use_quality": "tool_use_quality"})
    df["final_response_match"] = df["tool_use_quality"]  # same signal
    rec = an.recommend_config(df, [A, B])
    assert rec["A"] == "hi"  # A high has higher mean quality
    assert rec["B"] == "hi"


def test_build_report_is_markdown():
    df = _table()
    df["final_response_match"] = df["tool_use_quality"]
    df["savings_pct"] = [60.0, 50.0, 40.0, 30.0]
    md = an.build_report(df, [A, B], "exp1")
    assert "# DOE Analysis — exp1" in md
    assert "Main effects" in md
    assert "Recommended config" in md
