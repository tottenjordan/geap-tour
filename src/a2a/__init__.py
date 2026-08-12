"""A2A (Agent-to-Agent) integration for the coordinator — preview-optional.

Publishes an agent card describing the coordinator's real skills, provides a
``RemoteA2aAgent`` client path, and registers/discovers the agent through the
same Agent Registry client used for MCP servers (``src/registry.py``).

Everything here is PREVIEW-OPTIONAL: if the A2A / Agent Registry preview surface
is unavailable in the target project, callers degrade gracefully (a logged
"A2A preview not enabled — skipping" and ``None``/``[]``) rather than crashing a
live run.
"""

from src.a2a.agent_card import (
    SKILL_IDS,
    agent_card_dict,
    build_agent_card,
    serialize_agent_card,
)
from src.a2a.remote_agent import (
    A2AUnavailable,
    build_remote_coordinator,
    try_build_remote_coordinator,
)

__all__ = [
    "SKILL_IDS",
    "A2AUnavailable",
    "agent_card_dict",
    "build_agent_card",
    "build_remote_coordinator",
    "serialize_agent_card",
    "try_build_remote_coordinator",
]
