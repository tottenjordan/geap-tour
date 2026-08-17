"""Read back a user's persisted Memory Bank facts — proof that recall works.

After multi-turn traffic establishes a user's preferences (see
``src/traffic/generate_traffic.py`` — the ``CONVERSATIONS`` set is designed to
state a preference in one session and recall it in a later one), the coordinator
flushes those events into Vertex AI Memory Bank via ``save_memories_callback``.
This CLI retrieves the facts Memory Bank generated for a given ``user_id`` from
the deployed coordinator Agent Engine, demonstrating that cross-session recall
actually persisted.

Usage:
  uv run python -m src.eval.verify_memory --user-id alice
  uv run python -m src.eval.verify_memory --user-id alice --engine-id <ENGINE_ID>

Import-safe: the Vertex client is constructed lazily (only when no client is
injected), so this module imports without GCP credentials and is unit-testable
with a fake client.
"""

import argparse
import os

from src.config import AGENT_ENGINE_ID, GCP_PROJECT_ID, GCP_REGION

# ADK's VertexAiMemoryBankService scopes coordinator memories by app_name +
# user_id. On a *deployed* Agent Engine the runtime sets a session's ``app_name``
# to the engine's own reasoning-engine id (verified: a live session dict reports
# ``app_name == "<engine_id>"``), and both ``add_session_to_memory`` (write) and
# ``PreloadMemoryTool`` (read) use that scope. So memories must be scoped by the
# ENGINE ID, not the agent's Python name — reading under "coordinator_agent"
# silently returns nothing even when facts exist. See
# docs/notes/memory-bank-app-name-scope.md.
_ENGINE_SCOPED = object()  # sentinel: derive app_name from the engine id


def _default_engine_id() -> str:
    """Coordinator engine id: env override, else the shared config default."""
    return os.environ.get("COORDINATOR_AGENT_ID") or AGENT_ENGINE_ID


def _bare_engine_id(engine_id: str) -> str:
    """Reduce any bare id / short / full resource name to the bare engine id.

    That bare id is the ``app_name`` the deployed runtime uses for memory scope.
    """
    return engine_id.rsplit("/", 1)[-1]


def _engine_resource_name(engine_id: str) -> str:
    """Normalize a bare id / short / full name to ``reasoningEngines/<id>``.

    ADK's Memory Bank API accepts the short ``reasoningEngines/<id>`` form; a
    full ``projects/.../reasoningEngines/<id>`` resource name is passed through.
    """
    if engine_id.startswith(("projects/", "reasoningEngines/")):
        return engine_id
    return f"reasoningEngines/{engine_id}"


def _default_client():
    """Lazily construct a Vertex client (kept out of import time)."""
    import vertexai

    return vertexai.Client(project=GCP_PROJECT_ID, location=GCP_REGION)


def fetch_memories(
    user_id: str,
    *,
    engine_id: str | None = None,
    app_name=_ENGINE_SCOPED,
    client=None,
    page_size: int = 100,
) -> list[str]:
    """Return the Memory Bank facts persisted for ``user_id``.

    Args:
        user_id: The user whose memories to read.
        engine_id: Coordinator engine id (bare / short / full name). Defaults to
            ``COORDINATOR_AGENT_ID`` env or the shared config default.
        app_name: Memory scope app name. Defaults to the engine id — the scope the
            deployed runtime actually uses. Pass an explicit string to override,
            or ``None`` to scope by user only.
        client: Optional pre-built Vertex client (injected in tests). Built
            lazily when omitted.
        page_size: Max number of memories to retrieve.

    Returns:
        A list of fact strings (empty if the user has no persisted memories).
    """
    client = client or _default_client()
    engine_id = engine_id or _default_engine_id()
    name = _engine_resource_name(engine_id)
    if app_name is _ENGINE_SCOPED:
        app_name = _bare_engine_id(engine_id)

    scope = {"user_id": user_id}
    if app_name:
        scope["app_name"] = app_name

    facts: list[str] = []
    results = client.agent_engines.retrieve_memories(
        name=name,
        scope=scope,
        simple_retrieval_params={"page_size": page_size},
    )
    for item in results:
        memory = getattr(item, "memory", None) or item
        fact = getattr(memory, "fact", None)
        if fact:
            facts.append(fact)
    return facts


def render_memories(user_id: str, facts: list[str]) -> str:
    """Format retrieved memories (or a clear empty message) for printing."""
    if not facts:
        return f"No persisted memories found for user '{user_id}'."
    lines = [f"Persisted memories for user '{user_id}' ({len(facts)}):"]
    lines.extend(f"  - {fact}" for fact in facts)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read back a user's persisted Memory Bank facts.")
    parser.add_argument("--user-id", required=True, help="User id to look up.")
    parser.add_argument(
        "--engine-id",
        default=None,
        help="Coordinator engine id (default: COORDINATOR_AGENT_ID / config).",
    )
    parser.add_argument(
        "--app-name",
        default=None,
        help="Memory scope app name (default: the engine id, the runtime's scope).",
    )
    args = parser.parse_args(argv)

    facts = fetch_memories(
        args.user_id,
        engine_id=args.engine_id,
        # No --app-name → engine-scoped (the runtime default); explicit value wins.
        app_name=_ENGINE_SCOPED if args.app_name is None else args.app_name,
    )
    print(render_memories(args.user_id, facts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
