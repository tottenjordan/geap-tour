"""Tests for runtime skill discovery (src/skills/toolset.py) and its wiring.

Three properties, and each one is written so that it can actually fail:

* **The toolset is real.** ``GCPSkillRegistry.__init__`` performs no I/O (it
  reads env, optionally loads an mTLS cert, and validates its arguments —
  credentials are resolved lazily on the first request), and ``SkillToolset``
  only assembles tool objects. So these tests build the *real* objects and
  assert on them, instead of asserting that a mock was called: a fake registry
  that answers anything would prove nothing about the SDK surface we depend on
  (docs/notes/checks-that-cannot-detect-their-own-failure.md). For the same
  reason the failure path is triggered by a real error from the real class
  (an empty ``project_id``), not by a patched raiser.

* **Flag off is byte-identical.** ``ENABLE_SKILL_REGISTRY`` defaults off, and
  the safety property of the whole feature is that an unset flag leaves the
  coordinator exactly as it was. ``TestCoordinatorWiring`` proves it by
  rebuilding the coordinator module under both flag values and diffing the
  results, rather than trusting the default.

* **The publisher and the toolset agree on one location.** They are separate
  modules that talk to the same registry; if they resolve different locations
  the publisher writes skills where the agent never looks, and every offline
  test still passes. ``TestLocationIsOneSourceOfTruth`` moves the override and
  checks that *both* halves follow it.
"""

import asyncio
import importlib
import logging
import os
import types

import pytest
from google.adk.integrations.skill_registry import GCPSkillRegistry
from google.adk.tools.skill_toolset import SkillToolset

import src.config as cfg
import src.deploy.deploy_agents as da
import src.skills.toolset as toolset_mod
from src.deploy.deploy_agents import _build_config
from src.skills.toolset import get_skill_toolset


def _fake_agent(name="coordinator_agent"):
    return types.SimpleNamespace(name=name)


class TestGetSkillToolset:
    """The read half of the Skill Registry integration."""

    def test_returns_a_real_toolset_backed_by_the_gcp_registry(self):
        toolset = get_skill_toolset()

        assert isinstance(toolset, SkillToolset)
        registry = toolset._registry
        assert isinstance(registry, GCPSkillRegistry)
        assert registry.project_id == cfg.GCP_PROJECT_ID
        assert registry.location == cfg.SKILL_REGISTRY_LOCATION

    def test_no_code_executor_and_no_environment(self):
        """Instruction-only skills, deliberately — see src/skills/definitions.py.

        A ``code_executor`` (or an ``environment``) is what turns a skill's
        ``scripts/`` directory into running code. Our skills ship none, so
        wiring one would add a sandbox and an arbitrary-code-execution surface
        for no demo value. Pinned here so nobody "finishes" the integration by
        adding one without noticing what it opens.
        """
        toolset = get_skill_toolset()

        assert toolset._code_executor is None
        assert toolset._env is None

    def test_exposes_the_registry_search_and_load_tools(self):
        """The point of the toolset: discovery at run time, not a baked list.

        ``search_skills`` exists only when a registry is configured (ADK adds
        ``SearchSkillsTool`` conditionally), so its presence is the observable
        proof that the registry actually reached the toolset.
        """
        toolset = get_skill_toolset()
        assert toolset is not None

        names = {t.name for t in asyncio.run(toolset.get_tools())}
        assert {"search_skills", "list_skills", "load_skill"} <= names

    def test_a_construction_failure_is_none_plus_a_named_warning(self, caplog, monkeypatch):
        """The ``get_mcp_tools`` posture: degrade, but never silently.

        A project-less registry is the real ``ValueError`` the real
        ``GCPSkillRegistry`` raises, so this exercises the actual failure, not a
        stand-in for one. ``GOOGLE_CLOUD_PROJECT`` has to go with it: the SDK
        falls back to that env var when the argument is empty, so on a
        workstation that exports it (this one does) an empty ``project_id``
        quietly succeeds — which is also why :func:`get_skill_toolset` always
        passes an explicit project and location rather than letting the SDK
        guess.
        """
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

        with caplog.at_level(logging.WARNING):
            assert get_skill_toolset(project_id="") is None

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "a coordinator quietly running without skills must be visible"
        text = warnings[0].getMessage()
        assert "project_id" in text, f"the underlying error is not named: {text}"

    def test_an_unexpected_error_type_also_degrades(self, caplog):
        """Skill discovery is opt-in and optional; it must not break the import.

        The coordinator builds its tool list at module import, so anything that
        escapes here takes the whole agent down at container start. This is why
        the handler is broad — an older ADK raises ``ImportError``, the registry
        raises ``ValueError``, and a preview SDK's construction-time error types
        are not a stable contract.
        """

        def _boom(project_id, location):
            raise TypeError("GCPSkillRegistry() got an unexpected keyword argument")

        with caplog.at_level(logging.WARNING):
            assert get_skill_toolset(registry_factory=_boom) is None

        assert "unexpected keyword argument" in caplog.text

    def test_registry_factory_receives_project_and_location(self):
        """The seam the tests inject through carries the real config values."""
        seen = {}

        def _record(project_id, location):
            seen["project_id"] = project_id
            seen["location"] = location
            return GCPSkillRegistry(project_id=project_id, location=location)

        get_skill_toolset(registry_factory=_record)

        assert seen == {
            "project_id": cfg.GCP_PROJECT_ID,
            "location": cfg.SKILL_REGISTRY_LOCATION,
        }


class TestBuildSkillTools:
    """``coordinator_agent._build_skill_tools`` — the conditional tool, in isolation.

    Callable directly with its flag as an argument (the reason
    ``_build_memory_tools`` has that shape), so the three outcomes are testable
    without importing the agent under three env permutations.
    """

    def test_flag_off_returns_nothing_and_never_touches_the_registry(self, monkeypatch):
        import src.agents.coordinator_agent as coordinator

        def _boom():
            raise AssertionError("the registry must not be contacted when the flag is off")

        monkeypatch.setattr(coordinator, "get_skill_toolset", _boom)

        assert coordinator._build_skill_tools(enable=False) == []

    def test_flag_on_returns_the_toolset(self, monkeypatch):
        import src.agents.coordinator_agent as coordinator

        toolset = SkillToolset()
        monkeypatch.setattr(coordinator, "get_skill_toolset", lambda: toolset)

        assert coordinator._build_skill_tools(enable=True) == [toolset]

    def test_flag_on_with_an_unavailable_registry_still_yields_a_working_agent(self, monkeypatch):
        """``get_skill_toolset`` returning None (preview off) must not add a tool."""
        import src.agents.coordinator_agent as coordinator

        monkeypatch.setattr(coordinator, "get_skill_toolset", lambda: None)

        assert coordinator._build_skill_tools(enable=True) == []


def _reload_coordinator(monkeypatch, *, enable: bool, toolset=None):
    """Rebuild the coordinator module with ``ENABLE_SKILL_REGISTRY=enable``.

    A reload (not a fresh import) because the flag is read at import time, which
    is exactly the code path a deployed container takes. ``get_mcp_tools`` is
    left alone so the MCP half of the tool list is the real thing.
    """
    import src.agents.coordinator_agent as coordinator

    monkeypatch.setattr(cfg, "ENABLE_SKILL_REGISTRY", enable)
    monkeypatch.setattr(toolset_mod, "get_skill_toolset", lambda: toolset)
    return importlib.reload(coordinator)


def _agent_fields(agent):
    """Everything about the agent except its tool list.

    Callbacks defined in the module are fresh function objects after a reload, so
    they are compared by qualname; ``generate_content_config`` is a pydantic
    model and compares by value.
    """
    return (
        agent.name,
        agent.instruction,
        getattr(agent.before_agent_callback, "__qualname__", agent.before_agent_callback),
        getattr(agent.after_agent_callback, "__qualname__", agent.after_agent_callback),
        agent.generate_content_config,
    )


@pytest.fixture
def restore_coordinator():
    """Undo any reload done by a test, so the module other tests import is real."""
    yield
    import src.agents.coordinator_agent as coordinator

    importlib.reload(coordinator)


class TestCoordinatorWiring:
    """Flag off must be byte-identical; flag on must add exactly one tool."""

    # ``restore_coordinator`` is listed FIRST on purpose: fixture teardown runs
    # in reverse setup order, so requesting it before ``monkeypatch`` makes the
    # final reload happen *after* the flag patch is undone — otherwise the
    # module left behind for the rest of the suite is the patched one.
    def test_flag_off_and_flag_on_differ_by_exactly_the_skill_toolset(
        self, restore_coordinator, monkeypatch
    ):
        skill_toolset = SkillToolset()

        off = _reload_coordinator(monkeypatch, enable=False)
        off_tools = list(off.coordinator_agent.tools)
        off_fields = _agent_fields(off.coordinator_agent)

        on = _reload_coordinator(monkeypatch, enable=True, toolset=skill_toolset)
        on_tools = list(on.coordinator_agent.tools)
        on_fields = _agent_fields(on.coordinator_agent)

        # Nothing but the tool list may move.
        assert on_fields == off_fields
        # Flag off: no skill tool anywhere in the surface.
        assert [t for t in off_tools if isinstance(t, SkillToolset)] == []
        # Flag on: the same tools, in the same order, plus the toolset — the
        # empty list the helper returns when off splats to nothing.
        assert [type(t) for t in on_tools[:-1]] == [type(t) for t in off_tools]
        assert on_tools[-1] is skill_toolset

    def test_flag_on_but_registry_unavailable_keeps_the_default_surface(
        self, restore_coordinator, monkeypatch
    ):
        off = _reload_coordinator(monkeypatch, enable=False)
        off_types = [type(t) for t in off.coordinator_agent.tools]

        on = _reload_coordinator(monkeypatch, enable=True, toolset=None)

        assert [type(t) for t in on.coordinator_agent.tools] == off_types


class TestDeployEnvBaking:
    """The flag has to reach the container, or it is a local-only illusion.

    The coordinator picks its tool list at import time *inside* the deployed
    engine, so a flag exported in the operator's shell and not baked into
    ``env_vars`` produces a deployed agent with no skills while every local test
    says it has them — invisible until someone asks the live engine.
    """

    def test_flag_off_bakes_nothing(self):
        env = _build_config(_fake_agent())["env_vars"]

        assert "ENABLE_SKILL_REGISTRY" not in env
        assert "SKILL_REGISTRY_LOCATION" not in env

    def test_flag_on_bakes_the_flag_and_the_location(self, monkeypatch):
        monkeypatch.setattr(da, "ENABLE_SKILL_REGISTRY", True)
        monkeypatch.setattr(da, "SKILL_REGISTRY_LOCATION", "europe-west4")

        env = _build_config(_fake_agent())["env_vars"]

        assert env["ENABLE_SKILL_REGISTRY"] == "1"
        # The location rides along with the flag: the container's GCP_REGION is
        # not necessarily the registry location, and if the engine resolved a
        # different one than the publisher used it would find no skills.
        assert env["SKILL_REGISTRY_LOCATION"] == "europe-west4"


class TestLocationIsOneSourceOfTruth:
    """The publisher writes where the toolset reads — enforced, not assumed."""

    def test_default_location_agrees_with_the_publishers_resource_names(self):
        import src.skills.publish_skills as publisher

        toolset = get_skill_toolset()
        assert toolset is not None

        name = publisher.skill_resource_name("receipt-audit")
        assert f"/locations/{toolset._registry.location}/skills/" in name

    def test_an_override_moves_both_halves_together(self, monkeypatch):
        """Set ``SKILL_REGISTRY_LOCATION`` and rebuild both modules.

        This is the test that fails if one side is re-pointed at ``GCP_REGION``:
        the two constants are equal by default, so only an override can tell
        them apart.
        """
        import src.skills.publish_skills as publisher

        seen = {}

        def _record_client(**kwargs):
            seen.update(kwargs)
            return types.SimpleNamespace(skills=None)

        previous = os.environ.get("SKILL_REGISTRY_LOCATION")
        os.environ["SKILL_REGISTRY_LOCATION"] = "europe-west4"
        try:
            importlib.reload(cfg)
            reloaded_toolset = importlib.reload(toolset_mod)
            reloaded_publisher = importlib.reload(publisher)

            import agentplatform

            monkeypatch.setattr(agentplatform, "Client", _record_client)
            reloaded_publisher.build_client()

            toolset = reloaded_toolset.get_skill_toolset()
            assert toolset is not None

            assert toolset._registry.location == "europe-west4"
            assert seen["location"] == "europe-west4"
            assert "/locations/europe-west4/skills/" in (
                reloaded_publisher.skill_resource_name("receipt-audit")
            )
        finally:
            if previous is None:
                del os.environ["SKILL_REGISTRY_LOCATION"]
            else:
                os.environ["SKILL_REGISTRY_LOCATION"] = previous
            importlib.reload(cfg)
            importlib.reload(toolset_mod)
            importlib.reload(publisher)
