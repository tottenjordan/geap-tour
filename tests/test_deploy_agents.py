"""_build_config bakes the DOE factor env into the deployed engine's env_vars.

A deployed engine reads config at import time inside its container, so every
DOE factor must be forwarded through deploy_agents._build_config's env_vars.
"""

import types

import src.config as cfg
from src.deploy.deploy_agents import _build_config, _tagged_display_name


def _fake_agent(name="coordinator_agent"):
    return types.SimpleNamespace(name=name)


def test_tagged_display_name_appends_suffix():
    """--tag suffixes the agent's display name for console grouping."""
    assert _tagged_display_name(_fake_agent(), "demo1") == "coordinator_agent_demo1"
    assert _tagged_display_name(_fake_agent("router_agent"), "demo1") == "router_agent_demo1"


def test_tagged_display_name_no_tag_is_bare_name():
    """No tag (None or empty) leaves the display name unchanged."""
    assert _tagged_display_name(_fake_agent(), None) == "coordinator_agent"
    assert _tagged_display_name(_fake_agent(), "") == "coordinator_agent"


def test_build_config_uses_tagged_display_name():
    """A tagged display name flows through to the deploy config."""
    tagged = _tagged_display_name(_fake_agent(), "demo1")
    config = _build_config(_fake_agent(), display_name=tagged)
    assert config["display_name"] == "coordinator_agent_demo1"


def test_build_config_includes_doe_factor_env():
    config = _build_config(_fake_agent())
    env = config["env_vars"]

    # Per-agent + shared model factors.
    assert env["AGENT_MODEL"] == cfg.AGENT_MODEL
    assert env["COORDINATOR_MODEL"] == cfg.COORDINATOR_MODEL
    assert env["TRAVEL_MODEL"] == cfg.TRAVEL_MODEL
    assert env["EXPENSE_MODEL"] == cfg.EXPENSE_MODEL
    assert env["ROUTER_MODEL"] == cfg.ROUTER_MODEL

    # Router-boundary factors (stringified floats).
    assert env["COMPLEXITY_LOW"] == str(cfg.COMPLEXITY_LOW)
    assert env["COMPLEXITY_HIGH"] == str(cfg.COMPLEXITY_HIGH)
    assert env["MEDIUM_SPLIT"] == str(cfg.MEDIUM_SPLIT)
    assert env["HIGH_SPLIT"] == str(cfg.HIGH_SPLIT)

    # Prompt variant factor.
    assert env["PROMPT_VARIANT"] == cfg.PROMPT_VARIANT


def test_build_config_env_values_are_all_strings():
    env = _build_config(_fake_agent())["env_vars"]
    for key, value in env.items():
        assert isinstance(value, str), f"{key} env value must be a str, got {type(value)}"


def test_build_config_enables_agent_engine_telemetry():
    """Deployed engines must enable telemetry so agent-side OTel spans export."""
    env = _build_config(_fake_agent())["env_vars"]
    assert env["GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"] == "true"
