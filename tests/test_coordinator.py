"""Offline configuration tests for the deployed coordinator agent.

Validates that the coordinator wires the unified guardrail (with block telemetry)
as its before_agent_callback while keeping the Memory Bank after_agent_callback,
and that server-side Model Armor is attached via generate_content_config. No live
GCP or MCP connections required.
"""

from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.preload_memory_tool import PreloadMemoryTool

from src import config
from src.agents.caching_preload_memory_tool import CachingPreloadMemoryTool
from src.agents.coordinator_agent import (
    _build_memory_tools,
    coordinator_agent,
    save_memories_callback,
)
from src.armor.config import get_armored_generate_config, guardrail_with_telemetry


class TestCoordinatorCallbacks:
    def test_before_agent_callback_is_guardrail_wrapper(self):
        # The governance guardrail (telemetry-wrapping) is wired at the entry point.
        assert coordinator_agent.before_agent_callback is guardrail_with_telemetry

    def test_after_agent_callback_still_saves_memories(self):
        # Wiring the guardrail must not disturb the existing Memory Bank callback.
        assert coordinator_agent.after_agent_callback is save_memories_callback


class TestMemoryPreloadToolSelection:
    def test_no_memory_tool_when_bank_disabled(self):
        assert _build_memory_tools(enable_bank=False, enable_cache=False) == []
        # Cache flag is moot when the bank is off.
        assert _build_memory_tools(enable_bank=False, enable_cache=True) == []

    def test_stock_preload_tool_when_cache_disabled(self):
        tools = _build_memory_tools(enable_bank=True, enable_cache=False)
        assert len(tools) == 1
        assert type(tools[0]) is PreloadMemoryTool  # exactly the stock tool

    def test_caching_preload_tool_when_cache_enabled(self):
        tools = _build_memory_tools(enable_bank=True, enable_cache=True)
        assert len(tools) == 1
        assert isinstance(tools[0], CachingPreloadMemoryTool)
        # Subclass of PreloadMemoryTool ⇒ deploy._wants_memory() still detects it.
        assert isinstance(tools[0], PreloadMemoryTool)


class TestCoordinatorServerSideArmor:
    def test_generate_content_config_armor_matches_backbone(self):
        # Server-side Model Armor is attached only for Gemini-2.x backbones (region-
        # scoped templates aren't honored on the global 3.x/Claude path). The default
        # coordinator backbone is Gemini-3.x, so armor is omitted here; the client-side
        # guardrail (asserted in TestCoordinatorCallbacks) is the guaranteed layer.
        cfg = coordinator_agent.generate_content_config
        assert cfg is not None
        if config.COORDINATOR_MODEL.startswith(("gemini-2", "models/")):
            assert cfg.model_armor_config is not None
        else:
            assert cfg.model_armor_config is None

    def test_armor_present_for_gemini_2x_backbone(self):
        # A Gemini-2.x backbone gets the server-side templates.
        cfg = get_armored_generate_config("gemini-2.5-flash")
        assert cfg.model_armor_config is not None
        assert "templates/" in cfg.model_armor_config.prompt_template_name
        assert "templates/" in cfg.model_armor_config.response_template_name


class TestCoordinatorStructureUnchanged:
    def test_specialist_agent_tools_present(self):
        names = [t.agent.name for t in coordinator_agent.tools if isinstance(t, AgentTool)]
        assert "travel_agent" in names
        assert "expense_agent" in names
