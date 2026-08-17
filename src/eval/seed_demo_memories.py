"""Pre-seed curated Memory Bank personas for the live demo.

There is no explicit "save a memory" API — facts only exist after real multi-turn
sessions fire the deployed coordinator's ``save_memories_callback``
(``add_session_to_memory``) and the managed runtime's *async* fact-generation job
completes (up to ~2 min). Doing that live stalls a demo, so this driver seeds
vivid, single-domain preferences for several personas ahead of time, then polls
Memory Bank until each persona has facts. By demo time the console Memory Bank
view is already rich and ``verify_cross_session_recall`` returns ``RECALL: PASS``
instantly.

``alice``'s seeded facts deliberately match
``verify_cross_session_recall.DEFAULT_EXPECTED_SIGNALS`` (window / Delta /
Marriott) so the downstream live-recall demo passes unchanged.

Idempotent — Memory Bank merges/dedupes facts, so re-running is safe (it may
enrich, not duplicate).

Import-safe: the agent handle is built lazily (only when not injected), so this
module imports without GCP credentials and is unit-testable with fakes.

Usage:
  uv run python -m src.eval.seed_demo_memories --engine-id <ENGINE_ID>
  uv run python -m src.eval.seed_demo_memories --engine-id <ENGINE_ID> --dry-run
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.config import GCP_PROJECT_ID, GCP_REGION
from src.eval.verify_cross_session_recall import _drain_stream, _poll_for_facts
from src.eval.verify_memory import (
    _default_engine_id,
    render_memories,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


@dataclass(frozen=True)
class Persona:
    """A demo user, the preference turns to send, and the facts we expect back."""

    user_id: str
    messages: tuple[str, ...]
    signals: tuple[str, ...]


# Light, single-domain preference statements only (heavy multi-step prompts can
# empty-out on a cold probe engine). alice's signals MUST match
# verify_cross_session_recall.DEFAULT_EXPECTED_SIGNALS.
DEMO_PERSONAS: tuple[Persona, ...] = (
    Persona(
        user_id="alice",
        messages=(
            "Hi, I'm Alice. I always prefer window seats and Delta flights.",
            "Also remember I have a corporate rate at Marriott hotels.",
        ),
        signals=("window", "Delta", "Marriott"),
    ),
    Persona(
        user_id="dana",
        messages=(
            "Hi, I'm Dana. On international trips I always fly business class, and I prefer United.",
            "One more thing — I like aisle seats on long-haul flights.",
        ),
        signals=("business", "United", "aisle"),
    ),
    Persona(
        user_id="sam",
        messages=(
            "Hi, I'm Sam, employee ID EMP007. I keep my meal expenses under the per-diem limit.",
            "Also, please never book me on red-eye flights.",
        ),
        signals=("EMP007", "per-diem", "red-eye"),
    ),
)


def seed_persona(agent, persona: Persona, *, user_id: str | None = None) -> str:
    """Open one session and send every preference turn; return the session id."""
    uid = user_id or persona.user_id
    session = agent.create_session(user_id=uid)
    session_id = session["id"]
    for message in persona.messages:
        _drain_stream(agent, user_id=uid, session_id=session_id, message=message)
    return session_id


def run_seed(
    personas: Sequence[Persona] = DEMO_PERSONAS,
    *,
    agent=None,
    client=None,
    engine_id: str | None = None,
    poll_timeout_s: float = 180.0,
    poll_interval_s: float = 10.0,
    wait: bool = True,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> list[dict]:
    """Seed all personas, then poll Memory Bank until each has facts.

    Sessions for *all* personas are created first, so the async fact-generation
    for early personas progresses while later ones are still being seeded.

    Returns one row per persona:
    ``{"user_id", "session_id", "facts", "n_facts", "seeded"}`` where ``seeded``
    is ``True`` when facts were found (or when ``wait`` is ``False``).
    """
    if agent is None:
        import vertexai
        from vertexai import agent_engines

        # agent_engines.get needs the FULL projects/.../reasoningEngines/<id> name;
        # the bare engine_id also flows to _poll_for_facts → fetch_memories, which
        # applies the store-API reasoningEngines/<id> form itself.
        from src.eval.batch_eval import _resolve_agent_resource_name

        # init pins the regional endpoint (engines live in GCP_REGION); without it
        # create_session/stream_query hit the wrong endpoint and 404.
        vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
        agent = agent_engines.get(_resolve_agent_resource_name(engine_id or _default_engine_id()))

    # Phase 1: seed every persona's session up front.
    seeded_sessions = [(p, seed_persona(agent, p)) for p in personas]

    # Phase 2: poll each persona for generated facts.
    results: list[dict] = []
    for persona, session_id in seeded_sessions:
        facts: list[str] = []
        if wait:
            facts = _poll_for_facts(
                persona.user_id,
                engine_id=engine_id,
                client=client,
                poll_timeout_s=poll_timeout_s,
                poll_interval_s=poll_interval_s,
                sleep_fn=sleep_fn,
            )
        results.append(
            {
                "user_id": persona.user_id,
                "session_id": session_id,
                "facts": facts,
                "n_facts": len(facts),
                "seeded": bool(facts) or not wait,
            }
        )
    return results


def _select_personas(users: Sequence[str] | None) -> list[Persona]:
    """Return DEMO_PERSONAS, optionally filtered to the requested user ids."""
    if not users:
        return list(DEMO_PERSONAS)
    wanted = {u.lower() for u in users}
    return [p for p in DEMO_PERSONAS if p.user_id.lower() in wanted]


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: pre-seed curated Memory Bank personas; exit non-zero if any failed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--engine-id",
        default=None,
        help="Coordinator engine id (default: COORDINATOR_AGENT_ID / config).",
    )
    parser.add_argument(
        "--user",
        action="append",
        dest="users",
        metavar="USER_ID",
        help="Only seed this persona (repeatable; default: all).",
    )
    parser.add_argument(
        "--poll-timeout", type=float, default=180.0, help="Seconds to wait per persona for facts."
    )
    parser.add_argument(
        "--poll-interval", type=float, default=10.0, help="Seconds between store polls."
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Seed sessions but skip the fact-generation poll.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and make no engine/store calls.",
    )
    args = parser.parse_args(argv)

    personas = _select_personas(args.users)
    if not personas:
        known = ", ".join(p.user_id for p in DEMO_PERSONAS)
        print(f"No personas match {args.users}. Known personas: {known}")
        return 1

    if args.dry_run:
        print("DRY RUN — no engine or store calls will be made.")
        print(f"Engine: {args.engine_id or '<config default>'}")
        for persona in personas:
            print(
                f"  {persona.user_id}: {len(persona.messages)} turn(s); "
                f"expect signals {list(persona.signals)}"
            )
        return 0

    results = run_seed(
        personas,
        engine_id=args.engine_id,
        poll_timeout_s=args.poll_timeout,
        poll_interval_s=args.poll_interval,
        wait=not args.no_wait,
    )

    print(f"\nSeeded {len(results)} persona(s) on engine {args.engine_id or '<config default>'}:")
    for row in results:
        status = "OK" if row["seeded"] else "NO FACTS"
        print(f"\n[{status}] {row['user_id']}  (session {row['session_id']})")
        print(render_memories(row["user_id"], row["facts"]))

    all_seeded = all(row["seeded"] for row in results)
    print(f"\nSEED: {'PASS' if all_seeded else 'FAIL'}")
    return 0 if all_seeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
