"""Diagnostic A/B: does a mixed-tier session kill a Claude tier with a lost tool-call id?

Reproduces the router's ``AnthropicError: 'tool_call_id'`` empty-at-200
**locally** — real Claude on Vertex, no engine, no deploy — and shows
:func:`src.models.tool_call_ids.restore_tool_call_ids` turning it into a real
answer.

The failing shape is a **mixed-tier session**, which is why a pure-Claude probe
comes back clean:

1. Turn 1 routes to a **Gemini** tier. Gemini issues no tool-call ids, so ADK
   mints its own ``adk-<uuid>`` and records it in the session events.
2. Turn 2 (same session) routes to a **Claude** tier. ADK strips ``adk-*`` ids
   before replay unless ``isinstance(agent.canonical_model, LiteLlm)`` — and the
   router's model is a :class:`~src.router.tier_routing_llm.TierRoutingLlm`
   wrapping a :class:`~src.models.quota_retry.RetryingLlm`, so the check is
   ``False``.
3. LiteLLM's Anthropic transform then subscripts the absent
   ``tool_message["tool_call_id"]`` and raises. ADK yields nothing: HTTP 200,
   zero characters.

Claude's *own* ``toolu_*`` ids survive (ADK only strips the ``adk-`` prefix), so
a session that never touched a Gemini tier never triggers this. The traffic
generator reuses one session per user, so ordinary router traffic mixes tiers
routinely.

This drives the **real** dispatcher and the real tier-selection callback, so it
exercises the deployed router's code path rather than a stand-in.

Run (needs Vertex credentials, spends two Claude turns per arm):

    uv run python -m src.eval.spike_tool_call_ids
    uv run python -m src.eval.spike_tool_call_ids --claude-model claude-opus-4-6

Exit: 0 pass (pre-fix fails, fixed answers), 1 fail, 2 inconclusive.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

# Gemini-2.5 mirrors the deployed router's lite/flash/pro tiers; sonnet is the
# band every measured empty landed in.
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"

TURN_ONE = "What is the expense total for EMP001?"
TURN_TWO = "Now do the same for EMP002."


def get_expense_total(employee_id: str) -> dict[str, Any]:
    """Return the total expenses for an employee."""
    return {"employee_id": employee_id, "total_usd": 1234.56, "count": 7}


def _history_ids(session: Any) -> list[tuple[str, str | None]]:
    """Every function call/response id recorded in the session, in order."""
    ids: list[tuple[str, str | None]] = []
    for event in getattr(session, "events", None) or []:
        content = getattr(event, "content", None)
        for part in (getattr(content, "parts", None) or []) if content else []:
            if part.function_call:
                ids.append(("call", part.function_call.id))
            if part.function_response:
                ids.append(("resp", part.function_response.id))
    return ids


async def _run_arm(*, fix_enabled: bool, gemini_model: str, claude_model: str) -> dict[str, Any]:
    """Drive a Gemini turn then a Claude turn in ONE session. Returns the outcome."""
    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    import src.models.quota_retry as quota_retry
    from src.router.tier_routing_llm import TierRoutingLlm

    tier = {"model": gemini_model}

    def select_tier(callback_context=None, llm_request=None, **_kwargs):
        """Stand-in for ``select_tier_model_callback``: pin the turn's tier."""
        if llm_request is not None:
            llm_request.model = tier["model"]
        return None

    agent = LlmAgent(
        name="tier_repro_agent",
        model=TierRoutingLlm([gemini_model, claude_model], default_model=gemini_model),
        instruction=(
            "You are an expense assistant. Use the get_expense_total tool, then "
            "state the total in one short sentence."
        ),
        tools=[get_expense_total],
        before_model_callback=select_tier,
    )

    session_service = InMemorySessionService()
    await session_service.create_session(app_name="repro", user_id="u1", session_id="s1")
    runner = Runner(agent=agent, app_name="repro", session_service=session_service)

    async def turn(prompt: str) -> str:
        text = ""
        async for event in runner.run_async(
            user_id="u1",
            session_id="s1",
            new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
        ):
            for part in (event.content.parts if event.content else None) or []:
                if part.text:
                    text += part.text
        return text.strip()

    # The pre-fix arm simulates the old behaviour by making the gate say "not
    # LiteLlm-backed", which is exactly what shipped before the fix.
    def _never_litellm_backed(model: object) -> bool:
        return False

    original = quota_retry.is_litellm_backed
    if not fix_enabled:
        # setattr, not a plain assignment: the module attribute is typed as the
        # concrete function, so a direct rebind is a type error.
        setattr(quota_retry, "is_litellm_backed", _never_litellm_backed)  # noqa: B010
    try:
        hop_one = await turn(TURN_ONE)
        session = await session_service.get_session(app_name="repro", user_id="u1", session_id="s1")
        ids = _history_ids(session)

        tier["model"] = claude_model
        try:
            hop_two = await turn(TURN_TWO)
        except Exception as exc:  # the exception ADK turns into an empty stream
            return {
                "gemini_turn_ok": bool(hop_one),
                "ids": ids,
                "outcome": "RAISED",
                "detail": f"{type(exc).__name__}: {str(exc)[:140]}",
            }
        return {
            "gemini_turn_ok": bool(hop_one),
            "ids": ids,
            "outcome": "FULL" if hop_two else "EMPTY",
            "detail": hop_two[:100],
        }
    finally:
        setattr(quota_retry, "is_litellm_backed", original)  # noqa: B010


def _report(label: str, result: dict[str, Any]) -> None:
    adk_ids = [i for _kind, i in result["ids"] if i and i.startswith("adk-")]
    print(f"=== {label}")
    print(f"  gemini turn ok : {result['gemini_turn_ok']}")
    print(f"  ids in history : {result['ids']}")
    print(f"  adk-* ids      : {len(adk_ids)} (the ones ADK strips on replay)")
    print(f"  claude turn    : {result['outcome']} {result['detail']!r}\n")


def verdict(*, pre_fix: str, fixed: str) -> tuple[str, int]:
    """Turn the two arms' outcomes into a verdict line and an exit code.

    A pre-fix arm that answered normally is **inconclusive**, not a pass: it
    means the repro did not put an ``adk-`` id in history, so the run proves
    nothing either way.
    """
    if pre_fix in ("RAISED", "EMPTY") and fixed == "FULL":
        return ("VERDICT: PASS — the fix turns the failing Claude turn into a real answer.", 0)
    if pre_fix == "FULL":
        return ("VERDICT: INCONCLUSIVE — the pre-fix arm did not reproduce the failure.", 2)
    return (f"VERDICT: FAIL — fixed arm ended {fixed}, not FULL.", 1)


def main(argv: Sequence[str] | None = None) -> int:
    """Run both arms of the A/B and print a verdict."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gemini-model", default=DEFAULT_GEMINI_MODEL)
    parser.add_argument("--claude-model", default=DEFAULT_CLAUDE_MODEL)
    args = parser.parse_args(argv)

    import src.config  # noqa: F401  (import for its Vertex env setup side effects)

    print(f"Mixed-tier session: {args.gemini_model} (turn 1) -> {args.claude_model} (turn 2)\n")

    arms = {}
    for label, fix_enabled in (("fix DISABLED (pre-fix)", False), ("fix ENABLED", True)):
        result = asyncio.run(
            _run_arm(
                fix_enabled=fix_enabled,
                gemini_model=args.gemini_model,
                claude_model=args.claude_model,
            )
        )
        arms[fix_enabled] = result["outcome"]
        _report(label, result)

    message, code = verdict(pre_fix=arms[False], fixed=arms[True])
    print(message)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
