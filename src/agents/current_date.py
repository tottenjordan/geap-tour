"""Tell the model what day it is — on every request, outside the optimized prompts.

A model has no clock. Asked for "flights from SFO to JFK on June 15", the
coordinator called ``search_flights(date="2024-06-15")`` — a year from its training
data — found nothing against a 2026 inventory, and told the user there were no
flights. Asked the same about Los Angeles, it stopped and asked the user which
year. Both are real defects for a travel assistant (people say "June 15", not
"2027-06-15"), and they were three of the four ``final_response_match`` failures
that kept the weekly Eval Gate red.

**Why a ``before_model_callback`` and not an edit to ``INSTRUCTION``:**

* The agents' instructions are GEPA-optimized; the convention is to change them
  only by re-optimization. Appending to the *request* leaves ``agent.instruction``
  byte-identical, so the optimizer, the prompt audits, and every test that reads
  the instruction see no change.
* The date is computed **per request**, not at import. A deployed engine keeps a
  container for weeks (``min_instances`` 1-4); a date baked in at import would go
  stale exactly as silently as the model's own guess.
* ``append_instructions`` lands in the system-instruction channel, which every
  backbone here honours — native Gemini, and Claude via LiteLlm.

It is not state-injected (``{current_date}`` in the instruction) because the
router's instruction is an ``InstructionProvider`` callable, and ADK skips state
injection for those.
"""

from __future__ import annotations

from datetime import UTC, date, datetime


def _today() -> date:
    """UTC, deliberately: the engine has no notion of the caller's timezone, and a
    date that shifts with the container's locale would be a second silent input."""
    return datetime.now(UTC).date()


def current_date_instruction(today: date | None = None) -> str:
    """The sentence appended to every request's system instruction."""
    d = today or _today()
    return (
        f"Today's date is {d:%A, %B} {d.day}, {d.year} ({d.isoformat()}, UTC). "
        "Use it to resolve dates the user gives without a year, and relative dates "
        'such as "tomorrow" or "next Friday" — do not ask the user for the year. '
        "A date without a year means its next occurrence on or after today. "
        "Tools that take a date expect YYYY-MM-DD."
    )


def inject_current_date(callback_context=None, llm_request=None, **_kwargs):
    """``before_model_callback``: append today's date to the system instruction.

    Runs before every LLM hop; each hop's request is rebuilt from the agent's
    instruction, so the line appears exactly once per request. Returns ``None`` so
    the model call proceeds and any later callback in a list still runs.
    """
    if llm_request is not None:
        llm_request.append_instructions([current_date_instruction()])
    return None
