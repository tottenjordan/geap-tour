"""Registry integrity for src.doe.factors."""

import pytest

from src.doe.factors import (
    CHANNELS,
    FACTORS,
    FACTORS_BY_NAME,
    Factor,
    get_factors,
    requires_fresh_deploy,
)


def test_registry_has_seed_factors():
    names = {f.name for f in FACTORS}
    assert names == {
        "router_boundaries",
        "model_tier",
        "prompt_variant",
        "eval_fidelity",
        "memory_bank",
        "model_backend",
    }


def test_model_backend_factor_is_coordinator_only():
    # The bake-off factor isolates the *coordinator* backbone: Gemini flash
    # (coded -1) vs Anthropic Claude Sonnet (coded +1). It must move ONLY
    # COORDINATOR_MODEL — sub-agents stay put — so the main effect attributes
    # any delta to the coordinator model alone. Contrast with model_tier, which
    # moves all three model env vars together.
    f = FACTORS_BY_NAME["model_backend"]
    assert f.channel == "engine_env"  # flipping it needs a fresh engine deploy
    assert f.low_label == "gemini"
    assert f.high_label == "claude"
    assert f.assignment("gemini") == {"COORDINATOR_MODEL": "gemini-3.6-flash"}
    assert f.assignment("claude") == {"COORDINATOR_MODEL": "claude-sonnet-5"}
    # Only the coordinator env var moves — not TRAVEL_MODEL / EXPENSE_MODEL.
    for label in f.labels:
        assert set(f.assignment(label)) == {"COORDINATOR_MODEL"}
    assert requires_fresh_deploy([f]) is True


def test_memory_bank_factor_toggles_env():
    f = FACTORS_BY_NAME["memory_bank"]
    assert f.channel == "engine_env"  # flipping it needs a fresh engine deploy
    assert f.assignment(f.low_label) == {"ENABLE_MEMORY_BANK": "0"}
    assert f.assignment(f.high_label) == {"ENABLE_MEMORY_BANK": "1"}
    assert requires_fresh_deploy([f]) is True


def test_every_factor_is_valid():
    for f in FACTORS:
        assert f.channel in CHANNELS
        assert len(f.levels) == 2
        assert f.low_label == f.labels[0]
        assert f.high_label == f.labels[1]
        assert f.low_label != f.high_label


def test_env_levels_have_string_values():
    for f in FACTORS:
        if f.channel in ("engine_env", "runner_env"):
            for label in f.labels:
                for value in f.assignment(label).values():
                    assert isinstance(value, str)


def test_requires_fresh_deploy():
    # model_tier + prompt_variant are engine_env → need a deploy.
    assert requires_fresh_deploy(get_factors(["model_tier"])) is True
    assert requires_fresh_deploy(get_factors(["prompt_variant"])) is True
    # router_boundaries (runner_env) + eval_fidelity (param) → no deploy.
    assert requires_fresh_deploy(get_factors(["router_boundaries", "eval_fidelity"])) is False
    # mixed → True.
    assert requires_fresh_deploy(get_factors(["router_boundaries", "model_tier"])) is True


def test_get_factors_default_is_all():
    assert [f.name for f in get_factors()] == [f.name for f in FACTORS]


def test_get_factors_unknown_raises():
    with pytest.raises(KeyError):
        get_factors(["does_not_exist"])


def test_bad_channel_rejected():
    with pytest.raises(ValueError):
        Factor(name="x", channel="bogus", levels={"a": {"K": "1"}, "b": {"K": "2"}})


def test_wrong_level_count_rejected():
    with pytest.raises(ValueError):
        Factor(name="x", channel="param", levels={"only": {"scenario_count": 3}})


def test_non_string_env_value_rejected():
    with pytest.raises(ValueError):
        Factor(
            name="x",
            channel="engine_env",
            levels={"a": {"K": 1}, "b": {"K": 2}},
        )
