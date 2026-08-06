"""Router complexity boundaries are sourced from src.config and env-overridable.

src.router.complexity binds THRESHOLDS/MEDIUM_SPLIT/HIGH_SPLIT from src.config at
import time, so overriding the boundaries requires reloading BOTH modules. A
fixture reloads them with clean env on teardown to avoid cross-module pollution.
"""

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
