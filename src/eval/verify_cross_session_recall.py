"""Prove cross-session Memory Bank recall — the "watch it remember" demo.

The existing verification split leaves a gap. ``src/traffic/generate_traffic.py``
(``CONVERSATIONS``) sends every turn of a "do you remember?" conversation to the
*same* session, so the answer can come from the live session's context window
rather than a Memory Bank retrieval. ``src/eval/verify_memory.py`` reads the
persisted store directly, which proves persistence but isn't the agent recalling
in a live turn.

This driver closes the loop end-to-end against the deployed coordinator:

1. **Session A** — state preferences ("I prefer window seats and Delta"). Draining
   the stream fires ``save_memories_callback`` → ``add_session_to_memory()``.
2. **Poll** until Memory Bank has generated facts for the user (generation is
   async), corroborating via ``verify_memory.fetch_memories``.
3. **Session B** — a *brand-new* session for the same user asks a question that
   needs those preferences. Because B has no prior turns, any preference in the
   answer was surfaced from Memory Bank via the coordinator's ``PreloadMemoryTool``.

A ``RECALL: PASS`` means a preference set in session A came back in a separate
session B — genuine cross-session recall, not same-session context.

Import-safe: the Vertex client and agent handle are built lazily (only when not
injected), so this module imports without GCP credentials and is unit-testable
with fakes.

Usage:
  uv run python -m src.eval.verify_cross_session_recall --user-id alice
  uv run python -m src.eval.verify_cross_session_recall --user-id alice --engine-id <ENGINE_ID>
"""

from __future__ import annotations

import argparse
import time
from typing import TYPE_CHECKING

from src.config import GCP_PROJECT_ID, GCP_REGION
from src.eval.verify_memory import (
    _default_engine_id,
    fetch_memories,
    render_memories,
)
from src.traffic.generate_traffic import _extract_text

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# Demo-sensible defaults: preferences stated in A, then a probe in B that only a
# recalled preference could answer specifically.
DEFAULT_USER_ID = "alice"
DEFAULT_SEED_MESSAGES = (
    "Hi, I'm Alice. I always prefer window seats and Delta flights.",
    "Also remember I have a corporate rate at Marriott hotels.",
)
DEFAULT_PROBE_MESSAGE = "Book me a flight to New York — use my usual preferences."
DEFAULT_EXPECTED_SIGNALS = ("window", "Delta", "Marriott")


def _drain_stream(agent, *, user_id: str, session_id: str, message: str) -> str:
    """Send one turn and concatenate the visible assistant text from the stream."""
    response = agent.stream_query(user_id=user_id, session_id=session_id, message=message)
    return "".join(_extract_text(chunk) for chunk in response)


def _poll_for_facts(
    user_id: str,
    *,
    engine_id: str | None,
    client,
    poll_timeout_s: float,
    poll_interval_s: float,
    sleep_fn: Callable[[float], None],
) -> list[str]:
    """Poll ``fetch_memories`` until facts appear or the timeout elapses.

    Memory Bank generates facts asynchronously after a session is flushed, so a
    fresh probe can race ahead of generation. Returns the facts found (possibly
    empty if the timeout is hit — recall may still succeed if generation lands
    between the last poll and the probe).
    """
    deadline = poll_timeout_s
    elapsed = 0.0
    while True:
        facts = fetch_memories(user_id, engine_id=engine_id, client=client)
        if facts or elapsed >= deadline:
            return facts
        sleep_fn(poll_interval_s)
        elapsed += poll_interval_s


def run_cross_session_recall(
    user_id: str = DEFAULT_USER_ID,
    *,
    agent=None,
    client=None,
    engine_id: str | None = None,
    seed_messages: Sequence[str] | None = None,
    probe_message: str | None = None,
    expected_signals: Sequence[str] | None = None,
    poll_timeout_s: float = 120.0,
    poll_interval_s: float = 10.0,
    wait: bool = True,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict:
    """Drive session A → persistence → session B and report whether recall worked.

    Args:
        user_id: The user whose preferences are established and recalled.
        agent: A deployed-engine handle (``agent_engines.get(...)`` result). Built
            lazily from ``engine_id`` when omitted.
        client: Vertex client for ``fetch_memories`` corroboration. Built lazily
            when omitted.
        engine_id: Coordinator engine id (bare / short / full name); defaults to
            the ``verify_memory`` engine default (pinned ``AGENT_ENGINE_ID``).
        seed_messages: Session-A turns that establish preferences.
        probe_message: The single session-B turn that should elicit recall.
        expected_signals: Case-insensitive substrings whose presence in the probe
            response proves recall.
        poll_timeout_s / poll_interval_s: Bound the wait for async fact generation.
        wait: When ``False``, skip the persistence poll (probe immediately).
        sleep_fn: Injected for tests; defaults to ``time.sleep``.

    Returns:
        ``{"recalled", "session_a_id", "session_b_id", "facts", "probe_response"}``.
    """
    seeds = list(seed_messages if seed_messages is not None else DEFAULT_SEED_MESSAGES)
    probe = probe_message if probe_message is not None else DEFAULT_PROBE_MESSAGE
    signals = list(expected_signals if expected_signals is not None else DEFAULT_EXPECTED_SIGNALS)

    if agent is None:
        import vertexai
        from vertexai import agent_engines

        # agent_engines.get needs the FULL projects/.../reasoningEngines/<id> name;
        # the bare engine_id still flows to _poll_for_facts → fetch_memories, which
        # applies the store-API reasoningEngines/<id> form itself.
        from src.eval.batch_eval import _resolve_agent_resource_name

        # init pins the regional endpoint (engines live in GCP_REGION); without it
        # create_session/stream_query hit the wrong endpoint and 404.
        vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
        agent = agent_engines.get(_resolve_agent_resource_name(engine_id or _default_engine_id()))

    # --- Session A: establish preferences (fires save_memories_callback) ---
    sess_a = agent.create_session(user_id=user_id)
    sess_a_id = sess_a["id"]
    for message in seeds:
        _drain_stream(agent, user_id=user_id, session_id=sess_a_id, message=message)

    # --- Wait for Memory Bank to generate facts (async), corroborate the store ---
    facts: list[str] = []
    if wait:
        facts = _poll_for_facts(
            user_id,
            engine_id=engine_id,
            client=client,
            poll_timeout_s=poll_timeout_s,
            poll_interval_s=poll_interval_s,
            sleep_fn=sleep_fn,
        )

    # --- Session B: a brand-new session recalls via PreloadMemoryTool ---
    sess_b = agent.create_session(user_id=user_id)
    sess_b_id = sess_b["id"]
    if sess_b_id == sess_a_id:  # defensive: B must be a different session than A
        raise RuntimeError(
            f"session B id ({sess_b_id}) matches session A — not a cross-session test"
        )
    probe_response = _drain_stream(agent, user_id=user_id, session_id=sess_b_id, message=probe)

    lowered = probe_response.lower()
    recalled = any(sig.lower() in lowered for sig in signals)

    return {
        "recalled": recalled,
        "session_a_id": sess_a_id,
        "session_b_id": sess_b_id,
        "facts": facts,
        "probe_response": probe_response,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: run the cross-session recall demo and print a PASS/FAIL verdict."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default=DEFAULT_USER_ID, help="User id (default: alice).")
    parser.add_argument(
        "--engine-id",
        default=None,
        help="Coordinator engine id (default: COORDINATOR_AGENT_ID / config).",
    )
    parser.add_argument(
        "--probe",
        default=DEFAULT_PROBE_MESSAGE,
        help="Session-B message that should elicit recall.",
    )
    parser.add_argument(
        "--signal",
        action="append",
        dest="signals",
        metavar="TEXT",
        help="Substring proving recall (repeatable; default: window/Delta/Marriott).",
    )
    parser.add_argument(
        "--poll-timeout", type=float, default=120.0, help="Seconds to wait for facts."
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Skip the persistence poll (probe immediately after session A).",
    )
    args = parser.parse_args(argv)

    result = run_cross_session_recall(
        args.user_id,
        engine_id=args.engine_id,
        probe_message=args.probe,
        expected_signals=args.signals,
        poll_timeout_s=args.poll_timeout,
        wait=not args.no_wait,
    )

    print(f"Session A: {result['session_a_id']}")
    print(f"Session B: {result['session_b_id']}  (new session, same user)")
    print(render_memories(args.user_id, result["facts"]))
    print(f"\nProbe: {args.probe}")
    print(f"Response: {result['probe_response']}")
    verdict = "PASS" if result["recalled"] else "FAIL"
    print(f"\nRECALL: {verdict}")
    return 0 if result["recalled"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
