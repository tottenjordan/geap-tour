"""Remote A2A client for the coordinator agent — preview-optional.

Builds a ``google.adk.agents.remote_a2a_agent.RemoteA2aAgent`` pointing at the
coordinator's A2A endpoint so other agents can call it over A2A. The ADK/A2A
surface is still preview, so construction is wrapped: ``build_remote_coordinator``
raises a catchable :class:`A2AUnavailable` when the surface is missing, and
``try_build_remote_coordinator`` degrades to ``None`` + a logged skip.
"""

import logging

from src.a2a.agent_card import COORDINATOR_DESCRIPTION
from src.config import A2A_AGENT_NAME, coordinator_a2a_url

log = logging.getLogger(__name__)

SKIP_MESSAGE = "A2A preview not enabled — skipping"


class A2AUnavailable(RuntimeError):
    """Raised when the A2A / RemoteA2aAgent preview surface is unavailable."""


def _load_remote_agent_class():
    """Import ``RemoteA2aAgent`` lazily so import-time is preview-safe.

    Isolated into a helper so tests can monkeypatch the class without patching
    ADK internals.
    """
    from google.adk.agents.remote_a2a_agent import RemoteA2aAgent

    return RemoteA2aAgent


def _agent_card_url(endpoint: str) -> str:
    """Append the A2A well-known agent-card path to an endpoint base URL."""
    try:
        from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
    except ImportError:
        return endpoint
    return endpoint.rstrip("/") + AGENT_CARD_WELL_KNOWN_PATH


def build_remote_coordinator(url: str | None = None):
    """Build a ``RemoteA2aAgent`` client for the coordinator.

    Args:
      url: Optional A2A endpoint base URL. Defaults to
        ``config.coordinator_a2a_url()``.

    Returns:
      A ``RemoteA2aAgent`` targeting the coordinator's well-known agent-card URL.

    Raises:
      A2AUnavailable: if the RemoteA2aAgent surface cannot be imported or
        constructed (preview not enabled / incompatible SDK).
    """
    endpoint = url or coordinator_a2a_url()
    card_url = _agent_card_url(endpoint)
    try:
        remote_agent_cls = _load_remote_agent_class()
    except ImportError as exc:  # preview surface missing
        raise A2AUnavailable(f"RemoteA2aAgent import failed: {exc}") from exc
    try:
        return remote_agent_cls(
            name=A2A_AGENT_NAME,
            agent_card=card_url,
            description=COORDINATOR_DESCRIPTION,
        )
    except Exception as exc:  # incompatible signature / construction error
        raise A2AUnavailable(f"RemoteA2aAgent construction failed: {exc}") from exc


def try_build_remote_coordinator(url: str | None = None):
    """Best-effort variant: return the remote agent or ``None`` on any failure.

    Logs the standard preview-skip notice and never raises, so callers on a live
    path degrade gracefully.
    """
    try:
        return build_remote_coordinator(url)
    except Exception as exc:
        log.warning("%s (%s)", SKIP_MESSAGE, exc)
        return None
