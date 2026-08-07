"""Registry integrity for src.doe.factors."""

import pytest

from src.doe.factors import (
    CHANNELS,
    FACTORS,
    Factor,
    get_factors,
    requires_fresh_deploy,
)


def test_registry_has_four_seed_factors():
    names = {f.name for f in FACTORS}
    assert names == {"router_boundaries", "model_tier", "prompt_variant", "eval_fidelity"}


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
