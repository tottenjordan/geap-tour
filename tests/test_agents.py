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
    from src.agents.coordinator_agent import coordinator_agent

    assert coordinator_agent.name == "coordinator_agent"
    assert len(coordinator_agent.tools) >= 4  # 3 MCP toolsets + PreloadMemory


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
