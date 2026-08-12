"""Router complexity boundaries are sourced from src.config and env-overridable.

src.router.complexity binds THRESHOLDS/MEDIUM_SPLIT/HIGH_SPLIT from src.config at
import time, so overriding the boundaries requires reloading BOTH modules. A
fixture reloads them with clean env on teardown to avoid cross-module pollution.
"""

import asyncio
import importlib

import pytest

import src.config
import src.router.complexity

_BOUNDARY_VARS = ("COMPLEXITY_LOW", "COMPLEXITY_HIGH", "MEDIUM_SPLIT", "HIGH_SPLIT")


def _reload():
    importlib.reload(src.config)
    return importlib.reload(src.router.complexity)


@pytest.fixture
def reloaded_complexity(monkeypatch):
    """Yield a reload helper; restore clean defaults on teardown."""
    yield _reload
    for var in _BOUNDARY_VARS:
        monkeypatch.delenv(var, raising=False)
    _reload()


def test_default_cut_points(reloaded_complexity, monkeypatch):
    for var in _BOUNDARY_VARS:
        monkeypatch.delenv(var, raising=False)
    cx = reloaded_complexity()

    assert cx.THRESHOLDS == [0.30, 0.60]
    assert cx.score_to_model_tier(0.29) == "lite"
    assert cx.score_to_model_tier(0.30) == "flash"
    assert cx.score_to_model_tier(0.45) == "sonnet"
    assert cx.score_to_model_tier(0.60) == "pro"
    assert cx.score_to_model_tier(0.80) == "opus"


def test_overridden_cut_points_shift(reloaded_complexity, monkeypatch):
    # Widen the low tier and push the sub-splits up.
    monkeypatch.setenv("COMPLEXITY_LOW", "0.40")
    monkeypatch.setenv("COMPLEXITY_HIGH", "0.70")
    monkeypatch.setenv("MEDIUM_SPLIT", "0.55")
    monkeypatch.setenv("HIGH_SPLIT", "0.90")
    cx = reloaded_complexity()

    assert cx.THRESHOLDS == [0.40, 0.70]
    # 0.35 was "flash" under defaults; now below COMPLEXITY_LOW → "lite".
    assert cx.score_to_model_tier(0.35) == "lite"
    # 0.50 below the new MEDIUM_SPLIT (0.55) → "flash".
    assert cx.score_to_model_tier(0.50) == "flash"
    # 0.60 above MEDIUM_SPLIT but below COMPLEXITY_HIGH → "sonnet".
    assert cx.score_to_model_tier(0.60) == "sonnet"
    # 0.80 above COMPLEXITY_HIGH but below HIGH_SPLIT (0.90) → "pro".
    assert cx.score_to_model_tier(0.80) == "pro"
    # 0.95 above HIGH_SPLIT → "opus".
    assert cx.score_to_model_tier(0.95) == "opus"


def test_score_to_level_follows_thresholds(reloaded_complexity, monkeypatch):
    monkeypatch.setenv("COMPLEXITY_LOW", "0.40")
    monkeypatch.setenv("COMPLEXITY_HIGH", "0.70")
    cx = reloaded_complexity()

    assert cx._score_to_level(0.39) == "low"
    assert cx._score_to_level(0.40) == "medium"
    assert cx._score_to_level(0.69) == "medium"
    assert cx._score_to_level(0.70) == "high"


# --- Cost eval reacts to the router_boundaries factor (the #1 fix) ------------
# run_cost_efficiency_eval now routes via the 5-tier score_to_model_tier, so the
# DOE router_boundaries factor (COMPLEXITY_LOW/HIGH + MEDIUM_SPLIT/HIGH_SPLIT)
# actually moves cost. This is what was inert before — see
# docs/notes/doe-router-boundaries-inert.md.


@pytest.fixture
def reloaded_cost_eval(monkeypatch):
    """Reload config + complexity + complexity_metrics; restore on teardown.

    complexity_metrics binds ``score_to_model_tier`` and the model constants at
    import, so it must be reloaded *after* the boundary-carrying modules.
    """
    import src.eval.complexity_metrics as cm

    def _reload_all():
        importlib.reload(src.config)
        importlib.reload(src.router.complexity)
        return importlib.reload(cm)

    yield _reload_all
    for var in _BOUNDARY_VARS:
        monkeypatch.delenv(var, raising=False)
    _reload_all()


def _routed_cost(cm, monkeypatch, scores):
    """Run the cost eval with a stubbed classifier that echoes fixed scores."""
    async def _fake_classify(prompt):
        score = float(prompt)
        return src.router.complexity.ComplexityResult(
            level=src.router.complexity._score_to_level(score),
            score=score,
            reason="",
        )

    monkeypatch.setattr(cm, "classify_complexity", _fake_classify)
    cases = [{"prompt": str(s)} for s in scores]
    return asyncio.run(cm.run_cost_efficiency_eval(cases))


def test_cost_eval_responds_to_boundary_factor(reloaded_cost_eval, monkeypatch):
    # Scores that sit in the tiers the aggressive shift pushes down a level:
    #   0.45 -> sonnet (baseline) but flash (aggressive)
    #   0.85 -> opus   (baseline) but pro   (aggressive)
    scores = [0.45, 0.85]

    for var in _BOUNDARY_VARS:
        monkeypatch.delenv(var, raising=False)
    cm = reloaded_cost_eval()
    base = _routed_cost(cm, monkeypatch, scores)
    assert [c["tier"] for c in base["per_case"]] == ["sonnet", "opus"]

    monkeypatch.setenv("COMPLEXITY_LOW", "0.44")
    monkeypatch.setenv("MEDIUM_SPLIT", "0.60")
    monkeypatch.setenv("COMPLEXITY_HIGH", "0.80")
    monkeypatch.setenv("HIGH_SPLIT", "0.95")
    cm = reloaded_cost_eval()
    aggressive = _routed_cost(cm, monkeypatch, scores)
    assert [c["tier"] for c in aggressive["per_case"]] == ["flash", "pro"]

    # Cheaper tiers => strictly lower routed cost and higher savings.
    assert aggressive["routed_cost_usd"] < base["routed_cost_usd"]
    assert aggressive["savings_pct"] > base["savings_pct"]
