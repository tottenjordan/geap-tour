"""Tests for runtime skill discovery (src/skills/toolset.py) and its wiring.

Three properties, and each one is written so that it can actually fail:

* **The toolset is real.** ``GCPSkillRegistry.__init__`` makes no registry call
  — it validates its arguments and reads env, and credentials are resolved
  lazily on the first request — though it is not literally I/O-free: with a
  default client cert source it writes two tempfiles and loads a cert chain
  (``src/skills/toolset.py:_gcp_skill_registry``). ``SkillToolset``
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

* **The publisher and the toolset agree on one location.**
  ``TestLocationIsOneSourceOfTruth`` moves the override and checks that *both*
  halves follow it (rationale: ``src/config.py:SKILL_REGISTRY_LOCATION``).
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
        # ``skills_folder`` is None iff neither a folder nor an environment was
        # given — the public read of ``_env is None``.
        assert toolset.skills_folder is None

    def test_exposes_exactly_the_four_useful_tools(self):
        """The point of the toolset: discovery at run time, not a baked list.

        ``search_skills`` exists only when a registry is configured (ADK adds
        ``SearchSkillsTool`` conditionally), so its presence is the observable
        proof that the registry actually reached the toolset.

        Exact set, not a subset: ADK builds ``RunSkillScriptTool``
        unconditionally and declares it to the model even with no executor and
        no environment, so without the ``tool_filter`` the coordinator grows a
        fifth callable tool that — our skills being instruction-only — can only
        return ``SCRIPT_NOT_FOUND``. This fails if it comes back.
        """
        toolset = get_skill_toolset()
        assert toolset is not None

        names = {t.name for t in asyncio.run(toolset.get_tools())}
        assert names == {"list_skills", "load_skill", "load_skill_resource", "search_skills"}
        assert "run_skill_script" not in names

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
        # The type is what makes a broad handler diagnosable: ImportError (ADK
        # too old) vs TypeError (our call shape) vs ValueError (bad config) are
        # three different fixes, and a message-less exception renders as nothing
        # at all without it.
        assert "TypeError" in caplog.text

    def test_the_warning_survives_a_message_less_exception(self, caplog):
        """``raise ValueError()`` must not log an empty reason."""

        def _boom(project_id, location):
            raise ValueError()

        with caplog.at_level(logging.WARNING):
            assert get_skill_toolset(registry_factory=_boom) is None

        assert "ValueError" in caplog.text
        assert "publish_skills --list" in caplog.text, "no pointer to what to do next"

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

    def test_the_registry_class_itself_is_a_valid_factory(self):
        """``GCPSkillRegistry.__init__`` is keyword-only — call it that way.

        The most natural factory a caller can pass is the class itself. Called
        positionally it raises ``TypeError: __init__() takes 1 positional
        argument but 3 were given``, which the broad handler then reports as
        "Skill Registry toolset unavailable" — our own bug wearing an
        infrastructure failure's clothes, and indistinguishable from one in the
        log. This is the regression test for that call shape.
        """
        toolset = get_skill_toolset(registry_factory=GCPSkillRegistry)

        assert isinstance(toolset, SkillToolset)
        assert isinstance(toolset._registry, GCPSkillRegistry)
        assert toolset._registry.project_id == cfg.GCP_PROJECT_ID
        assert toolset._registry.location == cfg.SKILL_REGISTRY_LOCATION


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
def reload_coordinator(monkeypatch):
    """Rebuild the coordinator module under a chosen ``ENABLE_SKILL_REGISTRY``.

    A reload (not a fresh import) because the flag is read at import time, which
    is exactly the code path a deployed container takes. ``get_mcp_tools`` is
    left alone so the MCP half of the tool list is the real thing.

    **One fixture owns both the patching and the undo, deliberately.** The
    ordering here is load-bearing: the restoring reload has to run *after* the
    flag patches are dropped, or the module left behind for the rest of the
    session is the patched one — ``ENABLE_SKILL_REGISTRY`` stuck True and
    ``get_skill_toolset`` stuck as a stub. That used to be a convention (request
    a ``restore_coordinator`` fixture *before* ``monkeypatch``, so teardown ran
    in the right order), which meant a two-token argument swap silently corrupted
    every later test in the session with the whole suite still green. Owning the
    order inside one fixture makes it unspellable. The explicit ``undo()`` also
    keeps the failure recoverable: if a test's own reload raises, the module is
    still rebuilt clean rather than left broken.
    """
    import src.agents.coordinator_agent as coordinator

    real_get_skill_toolset = toolset_mod.get_skill_toolset

    def _reload(*, enable: bool, toolset=None):
        monkeypatch.setattr(cfg, "ENABLE_SKILL_REGISTRY", enable)
        monkeypatch.setattr(toolset_mod, "get_skill_toolset", lambda: toolset)
        return importlib.reload(coordinator)

    yield _reload

    monkeypatch.undo()  # drop the patches FIRST, explicitly...
    rebuilt = importlib.reload(coordinator)  # ...then rebuild the real module.
    # Belt and braces: assert the rebuild actually took, so a future edit that
    # reorders these two lines fails HERE — loudly, in the test that broke it —
    # instead of leaking a stubbed ``get_skill_toolset`` and a True flag into
    # every later test in the session with the suite still green.
    assert rebuilt.get_skill_toolset is real_get_skill_toolset


class TestCoordinatorWiring:
    """Flag off must be byte-identical; flag on must add exactly one tool."""

    def test_flag_off_and_flag_on_differ_by_exactly_the_skill_toolset(self, reload_coordinator):
        skill_toolset = SkillToolset()

        off = reload_coordinator(enable=False)
        off_tools = list(off.coordinator_agent.tools)
        off_fields = _agent_fields(off.coordinator_agent)

        on = reload_coordinator(enable=True, toolset=skill_toolset)
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

    def test_flag_on_but_registry_unavailable_keeps_the_default_surface(self, reload_coordinator):
        off = reload_coordinator(enable=False)
        off_types = [type(t) for t in off.coordinator_agent.tools]

        on = reload_coordinator(enable=True, toolset=None)

        assert [type(t) for t in on.coordinator_agent.tools] == off_types


class TestDeployEnvBaking:
    """The flag has to reach the container, or it is a local-only illusion.

    The coordinator picks its tool list at import time *inside* the deployed
    engine, so a flag exported in the operator's shell and not baked into
    ``env_vars`` produces a deployed agent with no skills while every local test
    says it has them — invisible until someone asks the live engine.
    """

    def test_flag_off_bakes_nothing(self, monkeypatch):
        # Pinned, not inherited: without this the test passes only because
        # nobody exported ENABLE_SKILL_REGISTRY=1, so a developer who has the
        # flag set in their shell gets a failure in a test about *baking*.
        monkeypatch.setattr(da, "ENABLE_SKILL_REGISTRY", False)

        env = _build_config(_fake_agent())["env_vars"]

        assert "ENABLE_SKILL_REGISTRY" not in env
        assert "SKILL_REGISTRY_LOCATION" not in env

    def test_flag_on_bakes_the_flag_and_the_location(self, monkeypatch):
        monkeypatch.setattr(da, "ENABLE_SKILL_REGISTRY", True)
        monkeypatch.setattr(da, "SKILL_REGISTRY_LOCATION", "europe-west4")

        env = _build_config(_fake_agent())["env_vars"]

        assert env["ENABLE_SKILL_REGISTRY"] == "1"
        # The location rides along with the flag — see src/config.py.
        assert env["SKILL_REGISTRY_LOCATION"] == "europe-west4"


@pytest.fixture
def registry_location_override():
    """Set ``SKILL_REGISTRY_LOCATION``, rebuild the three modules that read it, undo.

    Hand-rolled rather than ``monkeypatch.setenv``, and for the same reason
    ``reload_coordinator`` owns its own undo: the restoring reloads must run
    *after* the env var is restored, and monkeypatch's teardown cannot be ordered
    against a separate reload fixture. Owning set → yield → restore → reload in
    one place makes that ordering structural, so a later "cleanup" to
    ``monkeypatch.setenv`` can't silently leave the modules pointing at
    europe-west4 for the rest of the session.
    """
    import src.skills.publish_skills as publisher

    reloadable = (cfg, toolset_mod, publisher)

    def _apply(location: str):
        os.environ["SKILL_REGISTRY_LOCATION"] = location
        return tuple(importlib.reload(m) for m in reloadable)

    previous = os.environ.get("SKILL_REGISTRY_LOCATION")
    try:
        yield _apply
    finally:
        if previous is None:
            os.environ.pop("SKILL_REGISTRY_LOCATION", None)
        else:
            os.environ["SKILL_REGISTRY_LOCATION"] = previous
        for module in reloadable:
            importlib.reload(module)


class TestLocationIsOneSourceOfTruth:
    """The publisher writes where the toolset reads — enforced, not assumed.

    Rationale for the single constant: ``src/config.py:SKILL_REGISTRY_LOCATION``.
    """

    def test_default_location_agrees_with_the_publishers_resource_names(self):
        import src.skills.publish_skills as publisher

        toolset = get_skill_toolset()
        assert toolset is not None

        name = publisher.skill_resource_name("receipt-audit")
        assert f"/locations/{toolset._registry.location}/skills/" in name

    def test_an_override_moves_both_halves_together(self, monkeypatch, registry_location_override):
        """The two constants are equal by default, so only an override separates them.

        This is the test that fails if one side is re-pointed at ``GCP_REGION``.
        """
        seen = {}

        def _record_client(**kwargs):
            seen.update(kwargs)
            return types.SimpleNamespace(skills=None)

        _cfg, reloaded_toolset, reloaded_publisher = registry_location_override("europe-west4")

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
