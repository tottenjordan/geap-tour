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
# user_id; the app_name matches the agent's name.
DEFAULT_APP_NAME = "coordinator_agent"


def _default_engine_id() -> str:
    """Coordinator engine id: env override, else the shared config default."""
    return os.environ.get("COORDINATOR_AGENT_ID") or AGENT_ENGINE_ID


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
    app_name: str | None = DEFAULT_APP_NAME,
    client=None,
    page_size: int = 100,
) -> list[str]:
    """Return the Memory Bank facts persisted for ``user_id``.

    Args:
        user_id: The user whose memories to read.
        engine_id: Coordinator engine id (bare / short / full name). Defaults to
            ``COORDINATOR_AGENT_ID`` env or the shared config default.
        app_name: Memory scope app name. Set to ``None`` to scope by user only.
        client: Optional pre-built Vertex client (injected in tests). Built
            lazily when omitted.
        page_size: Max number of memories to retrieve.

    Returns:
        A list of fact strings (empty if the user has no persisted memories).
    """
    client = client or _default_client()
    name = _engine_resource_name(engine_id or _default_engine_id())

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
    parser = argparse.ArgumentParser(
        description="Read back a user's persisted Memory Bank facts."
    )
    parser.add_argument("--user-id", required=True, help="User id to look up.")
    parser.add_argument(
        "--engine-id",
        default=None,
        help="Coordinator engine id (default: COORDINATOR_AGENT_ID / config).",
    )
    parser.add_argument(
        "--app-name",
        default=DEFAULT_APP_NAME,
        help=f"Memory scope app name (default: {DEFAULT_APP_NAME}).",
    )
    args = parser.parse_args(argv)

    facts = fetch_memories(
        args.user_id, engine_id=args.engine_id, app_name=args.app_name
    )
    print(render_memories(args.user_id, facts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
