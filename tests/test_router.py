"""Tests for the 5-tier multi-model prompt router."""

import json

import pytest

from src.router.complexity import ComplexityResult, _score_to_level, score_to_model_tier
from src.router.cost_tracker import CostTracker, RequestLog, estimate_cost


class TestComplexityScoring:
    def test_low_score(self):
        assert _score_to_level(0.0) == "low"
        assert _score_to_level(0.15) == "low"
        assert _score_to_level(0.29) == "low"

    def test_medium_score(self):
        # DOE-tuned defaults: medium = [COMPLEXITY_LOW=0.44, COMPLEXITY_HIGH=0.80)
        assert _score_to_level(0.44) == "medium"
        assert _score_to_level(0.60) == "medium"
        assert _score_to_level(0.79) == "medium"

    def test_high_score(self):
        assert _score_to_level(0.80) == "high"
        assert _score_to_level(0.90) == "high"
        assert _score_to_level(1.0) == "high"

    def test_model_tier_lite(self):
        assert score_to_model_tier(0.0) == "lite"
        assert score_to_model_tier(0.15) == "lite"
        assert score_to_model_tier(0.43) == "lite"

    def test_model_tier_flash(self):
        # flash = [COMPLEXITY_LOW=0.44, MEDIUM_SPLIT=0.60)
        assert score_to_model_tier(0.44) == "flash"
        assert score_to_model_tier(0.50) == "flash"
        assert score_to_model_tier(0.59) == "flash"

    def test_model_tier_sonnet(self):
        # sonnet = [MEDIUM_SPLIT=0.60, COMPLEXITY_HIGH=0.80)
        assert score_to_model_tier(0.60) == "sonnet"
        assert score_to_model_tier(0.70) == "sonnet"
        assert score_to_model_tier(0.79) == "sonnet"

    def test_model_tier_pro(self):
        # pro = [COMPLEXITY_HIGH=0.80, HIGH_SPLIT=0.95)
        assert score_to_model_tier(0.80) == "pro"
        assert score_to_model_tier(0.88) == "pro"
        assert score_to_model_tier(0.94) == "pro"

    def test_model_tier_opus(self):
        assert score_to_model_tier(0.95) == "opus"
        assert score_to_model_tier(0.98) == "opus"
        assert score_to_model_tier(1.0) == "opus"

    def test_complexity_result_dataclass(self):
        r = ComplexityResult(level="high", score=0.85, reason="multi-step planning")
        assert r.level == "high"
        assert r.score == 0.85
        assert r.reason == "multi-step planning"


class TestCostTracker:
    def test_estimate_cost_flash_lite(self):
        cost = estimate_cost("gemini-2.5-flash-lite", 1_000_000, 1_000_000)
        assert cost == pytest.approx(0.075 + 0.30, rel=1e-4)

    def test_estimate_cost_flash(self):
        cost = estimate_cost("gemini-2.5-flash", 1_000_000, 1_000_000)
        assert cost == pytest.approx(0.15 + 0.60, rel=1e-4)

    def test_estimate_cost_pro(self):
        cost = estimate_cost("gemini-2.5-pro", 1_000_000, 1_000_000)
        assert cost == pytest.approx(1.25 + 10.00, rel=1e-4)

    def test_estimate_cost_sonnet(self):
        cost = estimate_cost("claude-sonnet-4-6", 1_000_000, 1_000_000)
        assert cost == pytest.approx(3.00 + 15.00, rel=1e-4)

    def test_estimate_cost_opus(self):
        cost = estimate_cost("claude-opus-4-6", 1_000_000, 1_000_000)
        assert cost == pytest.approx(15.00 + 75.00, rel=1e-4)

    def test_cost_ratio_lite_vs_opus(self):
        lite = estimate_cost("gemini-2.5-flash-lite", 200, 500)
        opus = estimate_cost("claude-opus-4-6", 200, 500)
        assert opus / lite > 100

    def test_cost_curve_is_monotonic(self):
        models = [
            "gemini-2.5-flash-lite",
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "claude-sonnet-4-6",
            "claude-opus-4-6",
        ]
        costs = [estimate_cost(m, 1000, 1000) for m in models]
        for i in range(len(costs) - 1):
            assert costs[i] < costs[i + 1], f"{models[i]} should be cheaper than {models[i + 1]}"

    def test_tracker_total(self, tmp_path):
        tracker = CostTracker(log_path=tmp_path / "test.jsonl")
        tracker.log_request(
            RequestLog(
                prompt="test",
                complexity_level="low",
                complexity_score=0.1,
                model_used="gemini-2.5-flash-lite",
                input_tokens=200,
                output_tokens=500,
                latency_ms=50,
                cost_usd=0.001,
            )
        )
        tracker.log_request(
            RequestLog(
                prompt="test2",
                complexity_level="high",
                complexity_score=0.9,
                model_used="claude-opus-4-6",
                input_tokens=200,
                output_tokens=500,
                latency_ms=2000,
                cost_usd=0.04,
            )
        )
        assert tracker.total_cost() == pytest.approx(0.041)
        assert len(tracker.cost_by_model()) == 2

    def test_tracker_jsonl_output(self, tmp_path):
        log_file = tmp_path / "test.jsonl"
        tracker = CostTracker(log_path=log_file)
        tracker.log_request(
            RequestLog(
                prompt="test",
                complexity_level="low",
                complexity_score=0.1,
                model_used="gemini-2.5-flash-lite",
                input_tokens=100,
                output_tokens=200,
                latency_ms=30,
                cost_usd=0.0001,
            )
        )
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["model_used"] == "gemini-2.5-flash-lite"
        assert data["complexity_level"] == "low"

    def test_generate_report(self, tmp_path):
        tracker = CostTracker(log_path=tmp_path / "test.jsonl")
        tracker.log_request(
            RequestLog(
                prompt="test",
                complexity_level="low",
                complexity_score=0.1,
                model_used="gemini-2.5-flash-lite",
                input_tokens=200,
                output_tokens=500,
                latency_ms=50,
                cost_usd=0.001,
            )
        )
        report = tracker.generate_report()
        assert "Cost Summary" in report
        assert "gemini-2.5-flash-lite" in report


class TestAgentConfig:
    def test_resolve_model_gemini(self):
        from src.config import resolve_model

        assert resolve_model("gemini-2.5-flash") == "gemini-2.5-flash"
        assert resolve_model("gemini-2.5-flash-lite") == "gemini-2.5-flash-lite"
        assert resolve_model("gemini-2.5-pro") == "gemini-2.5-pro"

    def test_resolve_model_litellm(self):
        from google.adk.models.lite_llm import LiteLlm

        from src.config import resolve_model

        result = resolve_model("claude-opus-4-6")
        assert isinstance(result, LiteLlm)
        result2 = resolve_model("claude-sonnet-4-6")
        assert isinstance(result2, LiteLlm)

    def test_router_has_no_sub_agents(self):
        # The router is now ONE direct-tools agent that swaps its model per tier.
        # It must NOT use sub_agents/transfer_to_agent: on the deployed Agent
        # Engine runtime the transferred specialist's turn never streamed back
        # (~0/8 full completions), the same wall the coordinator hit with nested
        # AgentTool MCP calls. See docs/notes/router-transfer-streaming.md.
        from src.router.agents import router_agent

        assert not router_agent.sub_agents

    def test_router_model_is_tier_dispatcher(self):
        from src.router.agents import router_agent
        from src.router.tier_routing_llm import TierRoutingLlm

        assert isinstance(router_agent.model, TierRoutingLlm)

    def test_router_holds_mcp_tools_directly(self):
        # The MCP toolsets are held directly on the root so their calls stream as
        # top-level events (the proven coordinator pattern).
        from src.router.agents import router_agent

        assert len(router_agent.tools) >= 3  # 3 MCP toolsets + PreloadMemoryTool

    def test_router_has_no_agent_tools(self):
        # Guardrail: no AgentTool sub-agent delegation should sneak back in — it's
        # the exact pattern that stalls on the deployed runtime.
        from google.adk.tools.agent_tool import AgentTool

        from src.router.agents import router_agent

        assert not any(isinstance(t, AgentTool) for t in router_agent.tools)

    def test_router_has_callback(self):
        from src.router.agents import router_agent

        assert router_agent.before_agent_callback is not None

    def test_router_has_model_select_callback(self):
        from src.router.agents import router_agent

        assert router_agent.before_model_callback is not None


class TestSelectTierModelCallback:
    def test_sets_request_model_from_state_tier(self):
        from types import SimpleNamespace

        from src.config import SONNET_MODEL
        from src.router.agents import select_tier_model_callback

        ctx = SimpleNamespace(state={"model_tier": "sonnet"})
        req = SimpleNamespace(model=None)
        select_tier_model_callback(callback_context=ctx, llm_request=req)
        assert req.model == SONNET_MODEL

    def test_no_tier_leaves_request_model_unchanged(self):
        from types import SimpleNamespace

        from src.router.agents import select_tier_model_callback

        ctx = SimpleNamespace(state={})
        req = SimpleNamespace(model="preset")
        select_tier_model_callback(callback_context=ctx, llm_request=req)
        assert req.model == "preset"

    def test_missing_args_are_noop(self):
        from src.router.agents import select_tier_model_callback

        assert select_tier_model_callback() is None


class TestTierInstructionProvider:
    def test_router_instruction_is_a_provider(self):
        # The router's instruction must be a callable provider (not a static str)
        # so each turn is served the chosen tier's prompt.
        from src.router.agents import router_agent, tier_instruction_provider

        assert router_agent.instruction is tier_instruction_provider
        assert callable(router_agent.instruction)

    def test_each_tier_gets_its_own_instruction(self):
        from types import SimpleNamespace

        from src.agents.flash_agent import INSTRUCTION as FLASH_INSTRUCTION
        from src.agents.lite_agent import INSTRUCTION as LITE_INSTRUCTION
        from src.agents.opus_agent import INSTRUCTION as OPUS_INSTRUCTION
        from src.agents.pro_agent import INSTRUCTION as PRO_INSTRUCTION
        from src.agents.sonnet_agent import INSTRUCTION as SONNET_INSTRUCTION
        from src.router.agents import tier_instruction_provider

        expected = {
            "lite": LITE_INSTRUCTION,
            "flash": FLASH_INSTRUCTION,
            "pro": PRO_INSTRUCTION,
            "sonnet": SONNET_INSTRUCTION,
            "opus": OPUS_INSTRUCTION,
        }
        for tier, instruction in expected.items():
            ctx = SimpleNamespace(state={"model_tier": tier})
            assert tier_instruction_provider(ctx) == instruction

    def test_no_tier_falls_back_to_generic_instruction(self):
        from types import SimpleNamespace

        from src.router.agents import ROUTER_INSTRUCTION, tier_instruction_provider

        assert tier_instruction_provider(SimpleNamespace(state={})) == ROUTER_INSTRUCTION

    def test_unknown_tier_falls_back_to_generic_instruction(self):
        from types import SimpleNamespace

        from src.router.agents import ROUTER_INSTRUCTION, tier_instruction_provider

        ctx = SimpleNamespace(state={"model_tier": "mystery"})
        assert tier_instruction_provider(ctx) == ROUTER_INSTRUCTION

    def test_missing_state_falls_back_to_generic_instruction(self):
        from types import SimpleNamespace

        from src.router.agents import ROUTER_INSTRUCTION, tier_instruction_provider

        # A ReadonlyContext with no usable state must not raise.
        assert tier_instruction_provider(SimpleNamespace()) == ROUTER_INSTRUCTION
