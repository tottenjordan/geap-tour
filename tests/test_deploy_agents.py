"""_build_config bakes the DOE factor env into the deployed engine's env_vars.

A deployed engine reads config at import time inside its container, so every
DOE factor must be forwarded through deploy_agents._build_config's env_vars.
"""

import types
from typing import ClassVar

import src.config as cfg
import src.deploy.deploy_agents as da
from src.deploy.deploy_agents import _build_config, _tagged_display_name


def _fake_agent(name="coordinator_agent"):
    return types.SimpleNamespace(name=name)


class _FakeAgentEngines:
    def create(self, **kwargs):
        return types.SimpleNamespace(
            resource_name="projects/p/locations/us-central1/reasoningEngines/999"
        )

    def update(self, **kwargs):
        return types.SimpleNamespace(
            resource_name="projects/p/locations/us-central1/reasoningEngines/999"
        )


class _FakeClient:
    def __init__(self):
        self.agent_engines = _FakeAgentEngines()


def _stub_deploy(monkeypatch, tmp_path, agent_name, set_name):
    """Point ENV_FILE at a tmp file and stub the deploy client + loader."""
    env = tmp_path / ".env"
    monkeypatch.setattr(da, "ENV_FILE", str(env))
    monkeypatch.setattr(da, "_get_client", lambda: _FakeClient())
    monkeypatch.setattr(da.vertexai, "init", lambda **k: None)
    # AdkApp's constructor resolves ADC (project/credentials) eagerly, which
    # blows up in credential-less CI. The fake client ignores whatever object
    # is passed to create/update, so wrap-to-identity keeps the deploy path
    # exercised without touching GCP auth.
    monkeypatch.setattr(da.agent_engines, "AdkApp", lambda agent, **k: agent)
    monkeypatch.setitem(
        da.AGENT_SETS[set_name],
        "loader",
        lambda: types.SimpleNamespace(name=agent_name, tools=[]),
    )
    return env


def test_coordinator_create_writes_agent_engine_id(monkeypatch, tmp_path):
    """A fresh coordinator deploy repoints AGENT_ENGINE_ID at its own id."""
    env = _stub_deploy(monkeypatch, tmp_path, "coordinator_agent", "coordinator")
    da.run_deploy(agent_set="coordinator", update=False)
    text = env.read_text()
    assert "COORDINATOR_AGENT_ID=999" in text
    assert "AGENT_ENGINE_ID=999" in text


def test_router_create_does_not_touch_agent_engine_id(monkeypatch, tmp_path):
    """Only the coordinator owns AGENT_ENGINE_ID — other agents leave it alone."""
    env = _stub_deploy(monkeypatch, tmp_path, "router_agent", "router")
    da.run_deploy(agent_set="router", update=False)
    text = env.read_text()
    assert "ROUTER_ENGINE_ID=999" in text
    assert "AGENT_ENGINE_ID=" not in text


def test_tagged_display_name_appends_suffix():
    """--tag suffixes the agent's display name for console grouping."""
    assert _tagged_display_name(_fake_agent(), "demo1") == "coordinator_agent_demo1"
    assert _tagged_display_name(_fake_agent("router_agent"), "demo1") == "router_agent_demo1"


def test_tagged_display_name_defaults_to_deploy_tag():
    """No explicit tag falls back to DEPLOY_TAG (jt1), so display names match
    the rest of this operator's engines and a plain --update never drops it."""
    import src.config as cfg

    assert _tagged_display_name(_fake_agent(), None) == f"coordinator_agent_{cfg.DEPLOY_TAG}"
    assert _tagged_display_name(_fake_agent(), "") == f"coordinator_agent_{cfg.DEPLOY_TAG}"


def test_tagged_display_name_explicit_tag_overrides_default():
    """An explicit --tag still wins over the DEPLOY_TAG default."""
    assert _tagged_display_name(_fake_agent(), "demo1") == "coordinator_agent_demo1"


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


def test_build_config_sets_resource_labels():
    """Deployed engines carry the default solution resource label."""
    assert _build_config(_fake_agent())["labels"] == cfg.RESOURCE_LABELS


def test_build_config_enables_agent_engine_telemetry():
    """Deployed engines must enable telemetry so agent-side OTel spans export."""
    env = _build_config(_fake_agent())["env_vars"]
    assert env["GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"] == "true"


def test_build_config_pins_full_trace_sampling():
    """Deployed engines pin 100% trace sampling (defensive against a future
    runtime default that could silently drop traces)."""
    env = _build_config(_fake_agent())["env_vars"]
    assert env["OTEL_TRACES_SAMPLER"] == "parentbased_traceidratio"
    assert env["OTEL_TRACES_SAMPLER_ARG"] == "1.0"


def test_build_config_omits_genai_upload_hook():
    """The genai completion-hook UPLOAD path must NOT be baked in: proven on a
    live native-gemini-3.7-flash coordinator in the managed runtime (2026-08-14)
    to capture ZERO content (no GCS JSONL over 55+ healthy streams) while adding
    ~6s median request latency, with no effect on the empty-stream failure rate
    (Fisher's exact p=1.0). See config.OTEL_ENV_VARS + the gemini3-native note.
    Guard against a well-meaning re-add."""
    env = _build_config(_fake_agent())["env_vars"]
    assert "OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK" not in env
    assert "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH" not in env
    assert "OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT" not in env


class _CapturingAdkApp:
    """Fake AdkApp that records the kwargs it was built with (no GCP/ADC)."""

    last_kwargs: ClassVar[dict] = {}

    def __init__(self, **kwargs):
        _CapturingAdkApp.last_kwargs = kwargs


def _memory_agent():
    """Agent that reads Memory Bank (holds a PreloadMemoryTool) — the coordinator."""
    from google.adk.tools.preload_memory_tool import PreloadMemoryTool

    return types.SimpleNamespace(name="coordinator_agent", tools=[PreloadMemoryTool()])


def test_analytics_plugin_disabled_returns_none(monkeypatch):
    """With the opt-in flag off, no plugin is built (and no BQ import happens)."""
    monkeypatch.setattr(da, "ENABLE_AGENT_ANALYTICS", False)
    assert da._analytics_plugin() is None


def test_build_app_wires_analytics_plugin_when_enabled(monkeypatch):
    """When enabled, _build_app forwards the analytics plugin to AdkApp."""
    sentinel = object()
    monkeypatch.setattr(da.agent_engines, "AdkApp", _CapturingAdkApp)
    monkeypatch.setattr(da, "_analytics_plugin", lambda: sentinel)

    da._build_app(_memory_agent())

    assert _CapturingAdkApp.last_kwargs.get("plugins") == [sentinel]


def test_build_app_no_plugins_when_disabled(monkeypatch):
    """When disabled, _build_app passes plugins=None (default runtime wrap)."""
    monkeypatch.setattr(da.agent_engines, "AdkApp", _CapturingAdkApp)
    monkeypatch.setattr(da, "_analytics_plugin", lambda: None)

    da._build_app(_memory_agent())

    assert _CapturingAdkApp.last_kwargs.get("plugins") is None


def test_build_app_relies_on_env_var_telemetry(monkeypatch):
    """_build_app must NOT pass enable_tracing=True to AdkApp.

    The in-process ``enable_tracing`` flag crashed/recycled the deployed worker
    mid-request (0 events streamed). Structural OTEL tracing is instead enabled
    by the managed runtime via the GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY
    env var (the GCP-recommended path, asserted in
    test_build_config_enables_agent_engine_telemetry), which populates the Cloud
    Trace span tree without the crashing flag.
    """
    monkeypatch.setattr(da.agent_engines, "AdkApp", _CapturingAdkApp)
    monkeypatch.setattr(da, "_analytics_plugin", lambda: None)

    da._build_app(_memory_agent())

    assert _CapturingAdkApp.last_kwargs.get("enable_tracing") is not True
    assert "enable_tracing" not in _CapturingAdkApp.last_kwargs
