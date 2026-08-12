"""Tests for env-var overridable agent-configuration factors in src.config.

Each override reloads src.config with monkeypatched env vars. A fixture reloads
src.config a final time with a clean environment on teardown so that other test
modules see the default configuration (avoids cross-module pollution).
"""

import importlib

import pytest

import src.config


@pytest.fixture
def reloaded_config(monkeypatch):
    """Yield a reload helper; restore clean defaults on teardown.

    monkeypatch auto-undoes any setenv/delenv, but the module-level constants in
    src.config are already bound, so we must reload once more after teardown env
    is clean to reset them for subsequent test modules.
    """
    def _reload():
        return importlib.reload(src.config)

    yield _reload

    # Ensure no override env vars leak; reload with the clean (post-undo) env.
    for var in (
        "COORDINATOR_MODEL",
        "TRAVEL_MODEL",
        "EXPENSE_MODEL",
        "ROUTER_MODEL",
        "COMPLEXITY_LOW",
        "COMPLEXITY_HIGH",
        "MEDIUM_SPLIT",
        "HIGH_SPLIT",
        "PROMPT_VARIANT",
    ):
        monkeypatch.delenv(var, raising=False)
    importlib.reload(src.config)


def test_defaults(reloaded_config, monkeypatch):
    for var in (
        "COORDINATOR_MODEL",
        "TRAVEL_MODEL",
        "EXPENSE_MODEL",
        "ROUTER_MODEL",
        "COMPLEXITY_LOW",
        "COMPLEXITY_HIGH",
        "MEDIUM_SPLIT",
        "HIGH_SPLIT",
        "PROMPT_VARIANT",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = reloaded_config()

    assert cfg.COORDINATOR_MODEL == cfg.AGENT_MODEL
    assert cfg.TRAVEL_MODEL == cfg.AGENT_MODEL
    assert cfg.EXPENSE_MODEL == cfg.AGENT_MODEL
    assert cfg.ROUTER_MODEL == cfg.LITE_MODEL
    assert cfg.COMPLEXITY_LOW == 0.44
    assert cfg.COMPLEXITY_HIGH == 0.80
    assert cfg.MEDIUM_SPLIT == 0.60
    assert cfg.HIGH_SPLIT == 0.95
    assert cfg.PROMPT_VARIANT == "gepa"


def test_model_overrides(reloaded_config, monkeypatch):
    monkeypatch.setenv("COORDINATOR_MODEL", "gemini-3.1-pro-preview")
    monkeypatch.setenv("TRAVEL_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("EXPENSE_MODEL", "gemini-3.1-flash-lite")
    monkeypatch.setenv("ROUTER_MODEL", "gemini-3.5-flash")
    cfg = reloaded_config()

    assert cfg.COORDINATOR_MODEL == "gemini-3.1-pro-preview"
    assert cfg.TRAVEL_MODEL == "claude-sonnet-4-6"
    assert cfg.EXPENSE_MODEL == "gemini-3.1-flash-lite"
    assert cfg.ROUTER_MODEL == "gemini-3.5-flash"


def test_boundary_overrides(reloaded_config, monkeypatch):
    monkeypatch.setenv("COMPLEXITY_LOW", "0.25")
    monkeypatch.setenv("COMPLEXITY_HIGH", "0.70")
    monkeypatch.setenv("MEDIUM_SPLIT", "0.40")
    monkeypatch.setenv("HIGH_SPLIT", "0.85")
    cfg = reloaded_config()

    assert cfg.COMPLEXITY_LOW == 0.25
    assert cfg.COMPLEXITY_HIGH == 0.70
    assert cfg.MEDIUM_SPLIT == 0.40
    assert cfg.HIGH_SPLIT == 0.85
    # Backwards-compat alias tracks COMPLEXITY_HIGH.
    assert cfg.COMPLEXITY_THRESHOLD_HIGH == 0.70


def test_prompt_variant_override(reloaded_config, monkeypatch):
    monkeypatch.setenv("PROMPT_VARIANT", "baseline")
    cfg = reloaded_config()

    assert cfg.PROMPT_VARIANT == "baseline"
