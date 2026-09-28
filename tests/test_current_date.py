"""The agents are told today's date on every request — and it reaches the model.

The defect: asked for flights "on June 15", the coordinator searched
``date="2024-06-15"`` (a year from its training data) and reported no flights
against a 2026 inventory; for Los Angeles it stopped to ask which year. See
``src/agents/current_date.py``.

The tests that matter are the ones that go through ADK: a callback attached to an
agent proves nothing if ADK never hands its edit to the model, or runs it once per
turn instead of once per hop.
"""

from __future__ import annotations

import ast
import importlib
from datetime import date
from pathlib import Path
from typing import ClassVar

import pytest
from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from src.agents import current_date
from src.agents.current_date import current_date_instruction, inject_current_date

_REPO = Path(__file__).resolve().parents[1]
_MARKER = "Today's date is"


class TestTheSentence:
    def test_names_the_day_in_both_forms(self):
        text = current_date_instruction(date(2026, 9, 28))
        assert "Monday, September 28, 2026" in text
        assert "2026-09-28" in text

    def test_says_which_year_a_bare_date_means(self):
        """Knowing today is not enough: "June 15" in September is ambiguous between
        this year (past) and next. A travel assistant means the next one."""
        assert "next occurrence on or after today" in current_date_instruction(date(2026, 9, 28))

    def test_tells_the_model_not_to_ask_for_the_year(self):
        assert "do not ask the user for the year" in current_date_instruction(date(2026, 1, 1))

    def test_the_date_is_read_per_call_not_at_import(self, monkeypatch):
        """Deployed containers live for weeks; an import-time date would go stale as
        silently as the model's guess did."""
        monkeypatch.setattr(current_date, "_today", lambda: date(2026, 9, 28))
        first = current_date_instruction()
        monkeypatch.setattr(current_date, "_today", lambda: date(2026, 9, 29))
        assert first != current_date_instruction()
        assert "2026-09-29" in current_date_instruction()


class TestTheCallback:
    def test_appends_to_an_existing_system_instruction(self):
        req = LlmRequest(config=types.GenerateContentConfig(system_instruction="BASE"))
        assert inject_current_date(callback_context=None, llm_request=req) is None
        si = req.config.system_instruction
        assert isinstance(si, str)
        assert si.startswith("BASE") and _MARKER in si

    def test_no_request_is_a_no_op(self):
        assert inject_current_date(callback_context=None, llm_request=None) is None


class _RecordingLlm(BaseLlm):
    """Records each request; first reply calls a tool so the turn has TWO hops."""

    model: str = "recording"
    seen: ClassVar[list[str]] = []

    async def generate_content_async(self, llm_request, stream=False):
        _RecordingLlm.seen.append(str(llm_request.config.system_instruction))
        if len(_RecordingLlm.seen) == 1:
            part = types.Part(function_call=types.FunctionCall(name="noop", args={}))
        else:
            part = types.Part(text="done")
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


def noop() -> str:
    """A tool, so the turn needs a second LLM hop."""
    return "ok"


class TestItReachesTheModelThroughAdk:
    async def test_every_hop_carries_the_date_exactly_once(self):
        from google.adk.runners import InMemoryRunner

        _RecordingLlm.seen = []
        agent = LlmAgent(
            model=_RecordingLlm(),
            name="probe",
            instruction="BASE INSTRUCTION",
            tools=[noop],
            before_model_callback=inject_current_date,
        )
        runner = InMemoryRunner(agent=agent, app_name="probe")
        session = await runner.session_service.create_session(app_name="probe", user_id="u")
        msg = types.Content(role="user", parts=[types.Part(text="flights on June 15")])
        async for _ in runner.run_async(user_id="u", session_id=session.id, new_message=msg):
            pass

        assert len(_RecordingLlm.seen) == 2, "the tool call should force a second hop"
        for si in _RecordingLlm.seen:
            assert "BASE INSTRUCTION" in si
            assert si.count(_MARKER) == 1, (
                "each hop's request must carry the date once — not zero (callback "
                "not reaching the model) and not accumulating across hops"
            )


def _agents_with_search_tools() -> list[str]:
    """Every agent module that hands the model `search_flights` — the date-taking tool."""
    mods = []
    for py in [*(_REPO / "src/agents").glob("*.py"), _REPO / "src/router/agents.py"]:
        src = py.read_text()
        if "SEARCH_MCP_SERVER" in src and "LlmAgent(" in src:
            mods.append(".".join(py.relative_to(_REPO).with_suffix("").parts))
    return sorted(mods)


class TestEveryAgentThatSearchesFlightsIsTold:
    def test_the_discovery_finds_the_known_agents(self):
        found = _agents_with_search_tools()
        assert "src.agents.coordinator_agent" in found
        assert "src.router.agents" in found
        assert len(found) >= 8, found

    @pytest.mark.parametrize("mod", _agents_with_search_tools())
    def test_the_callback_is_attached(self, mod):
        agent = importlib.import_module(mod).root_agent
        assert inject_current_date in agent.canonical_before_model_callbacks, (
            f"{mod} gives the model search_flights but never tells it today's date — "
            "it will search a training-data year"
        )

    def test_the_router_still_selects_its_tier_first(self):
        from src.router.agents import router_agent, select_tier_model_callback

        cbs = router_agent.canonical_before_model_callbacks
        assert cbs.index(select_tier_model_callback) < cbs.index(inject_current_date)


class TestTheOptimizedInstructionsAreUntouched:
    def test_no_instruction_constant_carries_a_date(self):
        """The point of doing this in a callback. A GEPA-optimized instruction is
        changed only by re-optimization, and a date baked into one goes stale."""
        for py in (_REPO / "src").rglob("*.py"):
            for node in ast.walk(ast.parse(py.read_text())):
                if (
                    isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and "INSTRUCTION" in t.id for t in node.targets)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    assert _MARKER not in node.value.value, py
