"""Offline configuration tests for the deployed coordinator agent.

Validates that the coordinator wires the unified guardrail (with block telemetry)
as its before_agent_callback while keeping the Memory Bank after_agent_callback,
and that server-side Model Armor is attached via generate_content_config. No live
GCP or MCP connections required.
"""

import types

from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from opentelemetry.trace import StatusCode

from src import config
from src.agents.caching_preload_memory_tool import CachingPreloadMemoryTool
from src.agents.coordinator_agent import (
    _build_memory_tools,
    coordinator_agent,
    save_memories_callback,
)
from src.armor.config import (
    get_armored_generate_config,
    guardrail_with_telemetry,
    server_side_armor_enabled,
)
from src.observability.tracing import traced


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


class TestCoordinatorHoldsNoAgentTools:
    """Delegation was removed by hand: 0 calls measured, and it can't stream.

    A trace census over 10 coordinator invocations recorded **zero** AgentTool
    calls — Section 1 of the instruction drives everything through the
    coordinator's own direct MCP tools. The AgentTools cost tool-definition
    tokens on every hop and, had one ever fired, would have landed on the
    documented non-streaming nested-MCP path.
    """

    def test_no_specialist_agent_tools(self):
        assert [t for t in coordinator_agent.tools if isinstance(t, AgentTool)] == []

    def test_instruction_names_no_agent_tool(self):
        """A named tool the agent does not hold is a guaranteed failed tool call."""
        from src.agents.coordinator_agent import INSTRUCTION

        assert "expense_agent" not in INSTRUCTION
        assert "travel_agent" not in INSTRUCTION
        assert "specialist agent tool" not in INSTRUCTION

    def test_instruction_still_covers_complex_expense_inquiries(self):
        """The capability Section 2 carried must survive its deletion."""
        from src.agents.coordinator_agent import INSTRUCTION

        assert "expense status" in INSTRUCTION
        assert "detailed expense reporting" in INSTRUCTION


class _FakeCallbackContext:
    """Minimal stand-in for ADK's CallbackContext used by the after-callback."""

    def __init__(self, *, raises: bool = False):
        self.session = types.SimpleNamespace(id="sess-1")
        self.user_id = "alice"
        self.saved = 0
        self._raises = raises

    async def add_session_to_memory(self):
        self.saved += 1
        if self._raises:
            raise RuntimeError("memory bank unavailable")


class TestSaveMemoriesCallbackTelemetry:
    """The after-callback is the coordinator's only domain-level trace surface.

    A Memory Bank *write* failure used to be swallowed whole by
    ``contextlib.suppress`` — no span, no metric, no log. And a trace could not
    say which backbone served the turn, mirroring what the router publishes on
    its ``router.route`` span.
    """

    async def test_a_successful_save_emits_a_span(self, span_exporter):
        ctx = _FakeCallbackContext()

        assert await save_memories_callback(ctx) is None

        span = next(
            s for s in span_exporter.get_finished_spans() if s.name == "coordinator.memory_save"
        )
        assert ctx.saved == 1
        assert span.status.status_code is not StatusCode.ERROR

    async def test_a_failed_save_is_recorded_but_still_suppressed(self, span_exporter):
        """The turn must not break — but the failure must stop being invisible."""
        ctx = _FakeCallbackContext(raises=True)

        assert await save_memories_callback(ctx) is None  # still swallowed

        span = next(
            s for s in span_exporter.get_finished_spans() if s.name == "coordinator.memory_save"
        )
        assert span.status.status_code is StatusCode.ERROR
        assert any(e.name == "exception" for e in span.events)

    async def test_config_attributes_land_on_the_enclosing_request_span(self, span_exporter):
        """``model.id`` is the operationally important one.

        The coordinator's backbone moves with ``COORDINATOR_MODEL`` (bake-off,
        DOE points, the 2.5 pin) and a trace could not tell you which one served
        a request without reading the ``generate_content`` span's *name*.
        """
        with traced("invoke_agent coordinator_agent"):
            await save_memories_callback(_FakeCallbackContext())

        span = next(
            s
            for s in span_exporter.get_finished_spans()
            if s.name == "invoke_agent coordinator_agent"
        )
        assert span.attributes["session.id"] == "sess-1"
        assert span.attributes["user.id"] == "alice"
        assert span.attributes["model.id"] == config.COORDINATOR_MODEL
        assert span.attributes["memory.enabled"] == config.ENABLE_MEMORY_BANK
        assert span.attributes["memory.cache_enabled"] == config.ENABLE_MEMORY_PRELOAD_CACHE
        assert span.attributes["armor.server_side"] == server_side_armor_enabled(
            config.COORDINATOR_MODEL
        )


class TestCoordinatorQuotaRetry:
    def test_coordinator_model_is_wrapped_for_quota_retry(self):
        """A Vertex 429 must not reach the client as an empty-at-200 stream.

        The router took 215 of them in two hours and answered with silence
        (docs/notes/router-empty-responses-quota.md). The coordinator has taken
        zero so far — this is insurance, and it costs one wrapper.
        """
        from src.models.quota_retry import RetryingLlm

        assert isinstance(coordinator_agent.model, RetryingLlm)

    def test_wrapper_still_reports_the_real_backbone_id(self):
        """``.model`` feeds billing, resource labels and the trace's model.id."""
        assert coordinator_agent.model.model.endswith(config.COORDINATOR_MODEL)
