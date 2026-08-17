"""Offline tests for Vertex Memory Bank + Session wiring in the coordinator deploy.

The coordinator reads Memory Bank (``PreloadMemoryTool``) and writes it
(``save_memories_callback``), but recall only persists across sessions if the
deployed Agent Engine is backed by managed Memory Bank + Session services. These
tests assert the deploy wraps memory-enabled agents in an ``AdkApp`` carrying
both service builders, leaves non-memory agents (e.g. the router) untouched, and
that the ``verify_memory`` helper reads persisted facts back for a user.

No live GCP or MCP connections required — the deploy client and ``AdkApp`` are
monkeypatched so no real Agent Engine call happens.
"""

import types
from typing import ClassVar

from google.adk.tools.preload_memory_tool import PreloadMemoryTool

import src.deploy.deploy_agents as da
from src.eval import verify_memory as vm


def _memory_agent(name="coordinator_agent"):
    """A minimal agent that reads Memory Bank (holds a PreloadMemoryTool)."""
    return types.SimpleNamespace(name=name, tools=[PreloadMemoryTool()])


def _plain_agent(name="router_agent"):
    """A minimal agent with no Memory Bank usage (e.g. the router)."""
    return types.SimpleNamespace(name=name, tools=[])


class _FakeAdkApp:
    """Records the kwargs the deploy passes to AdkApp."""

    instances: ClassVar[list["_FakeAdkApp"]] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeAdkApp.instances.append(self)


class _FakeAgentEngines:
    def __init__(self):
        self.create_kwargs = None
        self.update_kwargs = None

    def create(self, **kwargs):
        self.create_kwargs = kwargs
        return types.SimpleNamespace(
            resource_name="projects/p/locations/us-central1/reasoningEngines/999"
        )

    def update(self, **kwargs):
        self.update_kwargs = kwargs
        return types.SimpleNamespace(
            resource_name="projects/p/locations/us-central1/reasoningEngines/999"
        )


class _FakeClient:
    def __init__(self):
        self.agent_engines = _FakeAgentEngines()


class TestWantsMemory:
    def test_true_when_preload_memory_tool_present(self):
        assert da._wants_memory(_memory_agent()) is True

    def test_false_without_preload_memory_tool(self):
        assert da._wants_memory(_plain_agent()) is False

    def test_false_when_no_tools_attr(self):
        assert da._wants_memory(types.SimpleNamespace(name="x")) is False


class TestBuildApp:
    def test_wraps_memory_agent_with_both_builders(self, monkeypatch):
        _FakeAdkApp.instances.clear()
        monkeypatch.setattr(da.agent_engines, "AdkApp", _FakeAdkApp)
        agent = _memory_agent()
        app = da._build_app(agent)
        assert isinstance(app, _FakeAdkApp)
        assert app.kwargs["agent"] is agent
        assert app.kwargs["memory_service_builder"] is da._memory_service_builder
        assert app.kwargs["session_service_builder"] is da._session_service_builder

    def test_wraps_plain_agent_with_session_only(self, monkeypatch):
        """Non-memory agents still need a managed Session service scoped to
        their own runtime id, or stream_query fails with "Failed to create
        session". They get the session builder but NOT the memory builder."""
        _FakeAdkApp.instances.clear()
        monkeypatch.setattr(da.agent_engines, "AdkApp", _FakeAdkApp)
        agent = _plain_agent()
        app = da._build_app(agent)
        assert isinstance(app, _FakeAdkApp)
        assert app.kwargs["agent"] is agent
        assert app.kwargs["session_service_builder"] is da._session_service_builder
        assert "memory_service_builder" not in app.kwargs


class TestDeployWiring:
    def test_deploy_agent_passes_memory_backed_app(self, monkeypatch):
        _FakeAdkApp.instances.clear()
        client = _FakeClient()
        monkeypatch.setattr(da.agent_engines, "AdkApp", _FakeAdkApp)
        monkeypatch.setattr(da, "_get_client", lambda: client)

        resource = da.deploy_agent(_memory_agent())

        assert resource.endswith("999")
        passed = client.agent_engines.create_kwargs["agent"]
        assert isinstance(passed, _FakeAdkApp)
        assert passed.kwargs["memory_service_builder"] is da._memory_service_builder
        assert passed.kwargs["session_service_builder"] is da._session_service_builder

    def test_update_agent_passes_memory_backed_app(self, monkeypatch):
        _FakeAdkApp.instances.clear()
        client = _FakeClient()
        monkeypatch.setattr(da.agent_engines, "AdkApp", _FakeAdkApp)
        monkeypatch.setattr(da, "_get_client", lambda: client)

        da.update_agent(_memory_agent(), "123")

        passed = client.agent_engines.update_kwargs["agent"]
        assert isinstance(passed, _FakeAdkApp)
        assert passed.kwargs["session_service_builder"] is da._session_service_builder

    def test_deploy_agent_wraps_plain_agent_with_session(self, monkeypatch):
        """A raw agent deploys inside a session-only AdkApp (no memory)."""
        _FakeAdkApp.instances.clear()
        client = _FakeClient()
        monkeypatch.setattr(da.agent_engines, "AdkApp", _FakeAdkApp)
        monkeypatch.setattr(da, "_get_client", lambda: client)

        agent = _plain_agent()
        da.deploy_agent(agent)

        passed = client.agent_engines.create_kwargs["agent"]
        assert isinstance(passed, _FakeAdkApp)
        assert passed.kwargs["agent"] is agent
        assert passed.kwargs["session_service_builder"] is da._session_service_builder
        assert "memory_service_builder" not in passed.kwargs


class TestRuntimeEngineId:
    """The session/memory services must scope to the engine's OWN id at runtime.

    Regression for the "Failed to create session" bug: baking config
    AGENT_ENGINE_ID (a stale/other engine) made a freshly-created coordinator
    point its Session service at the wrong engine. Inside the container the
    runtime injects the engine's own id as GOOGLE_CLOUD_AGENT_ENGINE_ID.
    """

    def test_prefers_runtime_injected_own_id(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "4181778621234413568")
        assert da._runtime_engine_id() == "4181778621234413568"

    def test_falls_back_to_config_id_when_runtime_var_absent(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", raising=False)
        assert da._runtime_engine_id() == da.AGENT_ENGINE_ID

    def test_falls_back_when_runtime_var_empty(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "")
        assert da._runtime_engine_id() == da.AGENT_ENGINE_ID


class TestServiceBuilders:
    def test_memory_service_builder_returns_memory_bank_service(self):
        svc = da._memory_service_builder()
        assert type(svc).__name__ == "VertexAiMemoryBankService"

    def test_session_service_builder_returns_session_service(self):
        svc = da._session_service_builder()
        assert type(svc).__name__ == "VertexAiSessionService"

    def test_session_builder_scopes_to_runtime_own_id(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "4181778621234413568")
        svc = da._session_service_builder()
        assert svc._get_reasoning_engine_id("app") == "4181778621234413568"


class TestCoordinatorRegression:
    """The real coordinator must keep both ends of the Memory Bank path."""

    def test_coordinator_still_saves_memories(self):
        from src.agents.coordinator_agent import (
            coordinator_agent,
            save_memories_callback,
        )

        assert coordinator_agent.after_agent_callback is save_memories_callback

    def test_coordinator_has_preload_memory_tool(self):
        from src.agents.coordinator_agent import coordinator_agent

        assert any(isinstance(t, PreloadMemoryTool) for t in coordinator_agent.tools)

    def test_wants_memory_true_for_real_coordinator(self):
        from src.agents.coordinator_agent import coordinator_agent

        assert da._wants_memory(coordinator_agent) is True


class TestMemoryBankToggle:
    """The `memory_bank` DOE factor: ENABLE_MEMORY_BANK=0 deploys the coordinator
    without Memory Bank (no PreloadMemoryTool, no memory-save callback), so a run
    measures the recall uplift. Default (unset) keeps current behavior."""

    def test_default_on_wires_memory(self):
        from src.agents.coordinator_agent import (
            coordinator_agent,
            save_memories_callback,
        )

        assert any(isinstance(t, PreloadMemoryTool) for t in coordinator_agent.tools)
        assert coordinator_agent.after_agent_callback is save_memories_callback

    def test_disabled_removes_memory(self, monkeypatch):
        import importlib

        monkeypatch.setenv("ENABLE_MEMORY_BANK", "0")
        import src.config as cfg
        import src.registry as reg
        import src.agents.coordinator_agent as ca

        # src.registry must be reloaded between src.config and the agent module:
        # it binds MCP_SERVER_URLS from src.config at import time, and the agent
        # resolves its MCP tools through registry's URL fallback. Without the
        # registry reload the fallback map is keyed by collection-time (pre-
        # conftest) server names, so a credential-less environment (CI) misses
        # the fallback and re-raises. See tests/test_prompt_variant.py.
        importlib.reload(cfg)
        importlib.reload(reg)
        importlib.reload(ca)
        try:
            assert cfg.ENABLE_MEMORY_BANK is False
            assert not any(isinstance(t, PreloadMemoryTool) for t in ca.coordinator_agent.tools)
            assert ca.coordinator_agent.after_agent_callback is None
            assert da._wants_memory(ca.coordinator_agent) is False
        finally:
            monkeypatch.delenv("ENABLE_MEMORY_BANK", raising=False)
            importlib.reload(cfg)
            importlib.reload(reg)
            importlib.reload(ca)


# --- verify_memory helper ---------------------------------------------------


class _FakeMemory:
    def __init__(self, fact):
        self.fact = fact


class _FakeRetrieved:
    def __init__(self, fact):
        self.memory = _FakeMemory(fact)


class _FakeMemAgentEngines:
    def __init__(self, items):
        self._items = items
        self.calls = []

    def retrieve_memories(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self._items)


class _FakeMemClient:
    def __init__(self, items):
        self.agent_engines = _FakeMemAgentEngines(items)


class TestVerifyMemory:
    def test_returns_facts_for_user(self):
        client = _FakeMemClient(
            [_FakeRetrieved("prefers window seat"), _FakeRetrieved("United loyalty")]
        )
        facts = vm.fetch_memories("alice", engine_id="123", client=client)
        assert facts == ["prefers window seat", "United loyalty"]

    def test_empty_when_no_memories(self):
        client = _FakeMemClient([])
        assert vm.fetch_memories("bob", engine_id="123", client=client) == []

    def test_scopes_by_user_id_and_engine_name(self):
        client = _FakeMemClient([])
        vm.fetch_memories("carol", engine_id="123", client=client)
        call = client.agent_engines.calls[0]
        assert call["scope"]["user_id"] == "carol"
        assert "123" in call["name"]

    def test_default_app_name_is_engine_id(self):
        """Default scope app_name is the engine id — the deployed runtime's scope."""
        client = _FakeMemClient([])
        vm.fetch_memories("carol", engine_id="123", client=client)
        assert client.agent_engines.calls[0]["scope"]["app_name"] == "123"

    def test_full_resource_name_scopes_by_bare_engine_id(self):
        client = _FakeMemClient([])
        vm.fetch_memories(
            "carol",
            engine_id="projects/p/locations/us-central1/reasoningEngines/999",
            client=client,
        )
        assert client.agent_engines.calls[0]["scope"]["app_name"] == "999"

    def test_explicit_app_name_none_scopes_by_user_only(self):
        client = _FakeMemClient([])
        vm.fetch_memories("carol", engine_id="123", app_name=None, client=client)
        assert "app_name" not in client.agent_engines.calls[0]["scope"]

    def test_explicit_app_name_overrides_default(self):
        client = _FakeMemClient([])
        vm.fetch_memories("carol", engine_id="123", app_name="custom", client=client)
        assert client.agent_engines.calls[0]["scope"]["app_name"] == "custom"

    def test_skips_entries_with_missing_fact(self):
        client = _FakeMemClient([_FakeRetrieved(""), _FakeRetrieved("real fact")])
        assert vm.fetch_memories("dan", engine_id="123", client=client) == ["real fact"]

    def test_render_reports_empty_without_crash(self):
        out = vm.render_memories("erin", [])
        assert "erin" in out
        assert "no" in out.lower()

    def test_render_lists_facts(self):
        out = vm.render_memories("frank", ["a", "b"])
        assert "frank" in out
        assert "a" in out and "b" in out

    def test_module_imports_without_credentials(self):
        import importlib

        importlib.reload(vm)
        assert hasattr(vm, "fetch_memories")
