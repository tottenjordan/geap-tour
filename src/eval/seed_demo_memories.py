"""Pre-seed curated Memory Bank facts for the live demo — reliably.

We want the console **Vertex AI → Agent Engine → Memory Bank** view to be rich and
``verify_cross_session_recall`` to return ``RECALL: PASS`` at demo time. The
"organic" path — drive a multi-turn session so the deployed coordinator's
``save_memories_callback`` (``add_session_to_memory``) fires and the managed
runtime *distills* facts asynchronously — is unreliable for a demo: generation is
async (minutes of lag), a cold probe engine can stream an empty 200 (no content →
no facts), and the in-engine callback swallows its own errors, so a failed write
is invisible. Empirically it persisted **zero** retrievable facts on our engines.

So this driver writes each persona's facts **directly** via the Memory Bank
``create_memory`` API (synchronous, no distillation), scoped exactly like the
coordinator's own memories: ``{app_name=<engine_id>, user_id}`` — on a deployed
engine the runtime's own ``app_name`` is the reasoning-engine id, NOT the agent's
Python name, so scoping by the engine id is what makes the facts show in the
console and get retrieved by the coordinator's ``PreloadMemoryTool`` in a live
turn (the same read path ``verify_memory`` / ``verify_cross_session_recall`` use;
see docs/notes/memory-bank-app-name-scope.md). Idempotent: a fact already present
for a user is skipped, so re-running enriches rather than duplicates.

``alice``'s facts deliberately satisfy
``verify_cross_session_recall.DEFAULT_EXPECTED_SIGNALS`` (window / Delta /
Marriott) so the downstream live-recall demo passes unchanged.

Import-safe: the Vertex client is built lazily (only when not injected), so this
module imports without GCP credentials and is unit-testable with a fake client.

Usage:
  uv run python -m src.eval.seed_demo_memories --engine-id <ENGINE_ID>
  uv run python -m src.eval.seed_demo_memories --engine-id <ENGINE_ID> --dry-run
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.config import GCP_PROJECT_ID, GCP_REGION
from src.eval.verify_memory import (
    _bare_engine_id,
    _default_engine_id,
    _engine_resource_name,
    fetch_memories,
    render_memories,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class Persona:
    """A demo user and the explicit facts to persist for them."""

    user_id: str
    facts: tuple[str, ...]
    signals: tuple[str, ...]


# Vivid, single-domain, screenshot-friendly facts. alice's signals MUST match
# verify_cross_session_recall.DEFAULT_EXPECTED_SIGNALS (window / Delta / Marriott).
DEMO_PERSONAS: tuple[Persona, ...] = (
    Persona(
        user_id="alice",
        facts=(
            "Alice prefers window seats and flies Delta whenever possible.",
            "Alice has a corporate rate at Marriott hotels.",
        ),
        signals=("window", "Delta", "Marriott"),
    ),
    Persona(
        user_id="dana",
        facts=(
            "Dana flies business class on international trips and prefers United Airlines.",
            "Dana likes aisle seats on long-haul flights.",
        ),
        signals=("business", "United", "aisle"),
    ),
    Persona(
        user_id="sam",
        facts=(
            "Sam's employee ID is EMP007 and keeps meal expenses under the per-diem limit.",
            "Sam never wants to be booked on red-eye flights.",
        ),
        signals=("EMP007", "per-diem", "red-eye"),
    ),
)


def _default_client():
    """Lazily construct a region-scoped Vertex client (kept out of import time).

    Imports from both packages on purpose: ``vertexai.Client`` is deprecated in
    favour of ``agentplatform.Client``, but ``init`` is not — ``agentplatform.init``
    *is* ``vertexai.init`` (both re-export ``google.cloud.aiplatform.init``), so
    moving it would be pure churn. See docs/notes/agentplatform-client-migration.md.
    """
    import agentplatform
    import vertexai

    vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
    return agentplatform.Client(project=GCP_PROJECT_ID, location=GCP_REGION)


def create_persona_memories(
    client,
    persona: Persona,
    *,
    engine_id: str | None = None,
    app_name: str | None = None,
) -> list[str]:
    """Write a persona's facts directly to Memory Bank; return the facts created.

    Scopes memories by the engine id (the runtime's own ``app_name``) unless an
    explicit ``app_name`` is given, so the coordinator's ``PreloadMemoryTool``
    recalls them live. Idempotent: facts already persisted for the user
    (exact-string match) are skipped, so re-running enriches rather than duplicates.
    """
    eid = engine_id or _default_engine_id()
    name = _engine_resource_name(eid)
    scope_app = app_name if app_name is not None else _bare_engine_id(eid)
    scope = {"app_name": scope_app, "user_id": persona.user_id}
    existing = set(
        fetch_memories(persona.user_id, engine_id=eid, app_name=scope_app, client=client)
    )

    created: list[str] = []
    for fact in persona.facts:
        if fact in existing:
            continue
        client.agent_engines.create_memory(name=name, fact=fact, scope=scope)
        created.append(fact)
    return created


def run_seed(
    personas: Sequence[Persona] = DEMO_PERSONAS,
    *,
    client=None,
    engine_id: str | None = None,
    app_name: str | None = None,
    verify: bool = True,
) -> list[dict]:
    """Create every persona's facts directly, then read them back to confirm.

    Direct creation is synchronous, so unlike the session-distillation path there
    is no async lag or cold-engine empty-stream failure to poll around. Memories
    are scoped by the engine id (the runtime's ``app_name``) unless overridden.

    Returns one row per persona:
    ``{"user_id", "created", "facts", "n_facts", "seeded"}`` where ``facts`` is
    what Memory Bank returns for the user afterwards and ``seeded`` is ``True``
    when the user has persisted facts (or when ``verify`` is ``False``).
    """
    if client is None:
        client = _default_client()

    eid = engine_id or _default_engine_id()
    scope_app = app_name if app_name is not None else _bare_engine_id(eid)

    results: list[dict] = []
    for persona in personas:
        created = create_persona_memories(client, persona, engine_id=eid, app_name=scope_app)
        facts = (
            fetch_memories(persona.user_id, engine_id=eid, app_name=scope_app, client=client)
            if verify
            else list(created)
        )
        results.append(
            {
                "user_id": persona.user_id,
                "created": created,
                "facts": facts,
                "n_facts": len(facts),
                "seeded": bool(facts) or not verify,
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
    """CLI: pre-seed curated Memory Bank facts; exit non-zero if any failed."""
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
        "--app-name",
        default=None,
        help="Memory scope app name (default: the engine id, the runtime's scope).",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip the read-back confirmation after creating facts.",
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
            print(f"  {persona.user_id}: {len(persona.facts)} fact(s)")
            for fact in persona.facts:
                print(f"      - {fact}")
        return 0

    results = run_seed(
        personas,
        engine_id=args.engine_id,
        app_name=args.app_name,
        verify=not args.no_verify,
    )

    print(f"\nSeeded {len(results)} persona(s) on engine {args.engine_id or '<config default>'}:")
    for row in results:
        status = "OK" if row["seeded"] else "NO FACTS"
        print(f"\n[{status}] {row['user_id']}  ({len(row['created'])} new)")
        print(render_memories(row["user_id"], row["facts"]))

    all_seeded = all(row["seeded"] for row in results)
    print(f"\nSEED: {'PASS' if all_seeded else 'FAIL'}")
    return 0 if all_seeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
