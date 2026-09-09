"""Tests for agent configurations — validates structure without requiring GCP."""


def test_travel_agent_config():
    from src.agents.travel_agent import travel_agent

    assert travel_agent.name == "travel_agent"
    assert len(travel_agent.tools) == 2


def test_expense_agent_config():
    from src.agents.expense_agent import expense_agent

    assert expense_agent.name == "expense_agent"
    assert len(expense_agent.tools) == 1


def test_coordinator_agent_config():
    """Exact tool count, not ``>= 4``.

    The tool surface is an input to ``tool_use_accuracy`` and therefore to the
    monitored ``agent_eval/*`` series, and a ``>=`` bound cannot see a tool being
    added (the opt-in Skill Registry toolset is the live example) — only one
    disappearing. This is the flag-OFF surface: 3 MCP toolsets + PreloadMemory,
    with ``ENABLE_SKILL_REGISTRY`` at its default off.
    """
    from google.adk.tools.skill_toolset import SkillToolset

    from src.agents.coordinator_agent import coordinator_agent

    assert coordinator_agent.name == "coordinator_agent"
    assert len(coordinator_agent.tools) == 4
    assert [t for t in coordinator_agent.tools if isinstance(t, SkillToolset)] == []


def test_coordinator_tool_surface_with_skill_registry_enabled(monkeypatch):
    """Flag ON adds exactly one tool, the ``SkillToolset`` — nothing else.

    Paired with the exact count above, this pins the surface in both directions.
    The stronger property — that flag-off is byte-identical to the pre-feature
    agent, ordering and every other field included — is proved by rebuilding the
    module under both flag values in
    ``tests/test_skill_toolset.py::TestCoordinatorWiring``.
    """
    from google.adk.tools.skill_toolset import SkillToolset

    from src.agents import coordinator_agent as mod

    toolset = SkillToolset()
    monkeypatch.setattr(mod, "get_skill_toolset", lambda: toolset)

    tools = [*mod.coordinator_agent.tools, *mod._build_skill_tools(enable=True)]

    assert len(tools) == 5
    assert tools[-1] is toolset


def test_every_agent_disables_afc():
    """AFC is on by default in google-genai and ADK never turns it off, so each
    agent carries the switch on its own generate_content_config. Pinning the
    wiring here — not just the factory — is what catches a dropped kwarg.
    See docs/notes/genai-afc-warning.md.
    """
    from src.agents.coordinator_agent import coordinator_agent
    from src.agents.expense_agent import expense_agent
    from src.agents.travel_agent import travel_agent

    for agent in (coordinator_agent, travel_agent, expense_agent):
        cfg = agent.generate_content_config
        assert cfg is not None, f"{agent.name} has no generate_content_config"
        assert cfg.automatic_function_calling.disable is True, agent.name


def test_coordinator_holds_no_agent_tools():
    """Direct tools only — delegation measured 0 calls and cannot stream."""
    from google.adk.tools.agent_tool import AgentTool

    from src.agents.coordinator_agent import coordinator_agent

    assert [t for t in coordinator_agent.tools if isinstance(t, AgentTool)] == []
