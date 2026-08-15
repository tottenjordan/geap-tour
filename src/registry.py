"""Agent Registry integration — discovers MCP servers by registered name.

Falls back to direct Cloud Run URLs when the Agent Registry entry is not found.
"""

import logging

from google.adk.integrations.agent_registry import AgentRegistry
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

from src.config import AGENT_REGISTRY_LOCATION, GCP_PROJECT_ID, MCP_SERVER_URLS

log = logging.getLogger(__name__)

# Default 5s connection / 300s read is too slow for Cloud Run MCP servers
MCP_TIMEOUT_SECONDS = 60.0
MCP_READ_TIMEOUT_SECONDS = 90.0

_registry = None


def get_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        _registry = AgentRegistry(project_id=GCP_PROJECT_ID, location=AGENT_REGISTRY_LOCATION)
    return _registry


def get_mcp_tools(server_name: str):
    try:
        toolset = get_registry().get_mcp_toolset(server_name)
        # Agent Registry uses default 5s/300s — override for Cloud Run
        if hasattr(toolset, "_connection_params"):
            if hasattr(toolset._connection_params, "timeout"):
                toolset._connection_params.timeout = MCP_TIMEOUT_SECONDS
            if hasattr(toolset._connection_params, "sse_read_timeout"):
                toolset._connection_params.sse_read_timeout = MCP_READ_TIMEOUT_SECONDS
        return toolset
    except (RuntimeError, ValueError) as exc:
        # ADK raises RuntimeError on registry control-plane HTTP/creds errors and
        # ValueError when the resolved entry has no endpoint URI — both mean the
        # registry couldn't hand us a usable toolset, so fall back to the direct
        # Cloud Run URL. Log at WARNING (not INFO): a coordinator quietly running
        # on the fallback path is exactly the silent degradation we want visible.
        url = MCP_SERVER_URLS.get(server_name)
        if not url:
            raise
        log.warning(
            "Agent Registry resolution failed for %s (%s) — falling back to direct URL %s",
            server_name,
            exc,
            url,
        )
        return McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url=url, timeout=MCP_TIMEOUT_SECONDS, sse_read_timeout=MCP_READ_TIMEOUT_SECONDS
            )
        )


# --- A2A agents (preview-optional) -----------------------------------------
# Registration/discovery of A2A agents reuses the SAME AgentRegistry client as
# the MCP-server flow above. The create endpoint is a preview surface that may
# not exist in every project, so both helpers degrade gracefully (logged skip +
# None/[]) instead of raising, mirroring the MCP direct-URL fallback posture.
A2A_PREVIEW_SKIP = "A2A preview not enabled — skipping"


def _slug(name: str) -> str:
    """Lowercase, hyphen-safe agent id derived from a display name."""
    cleaned = "".join(c if c.isalnum() else "-" for c in name.lower())
    return "-".join(part for part in cleaned.split("-") if part) or "agent"


def register_a2a_agent(card, agent_id: str | None = None):
    """Register an A2A agent card in Agent Registry (preview-optional).

    Accepts either an ``a2a.types.AgentCard`` or an already-serialized dict.
    Reuses ``get_registry()`` and its ``_make_request`` transport so auth/mTLS
    are shared with the MCP flow. Returns the created resource dict, or ``None``
    (logged skip) if the preview surface / credentials are unavailable.
    """
    try:
        from src.a2a.agent_card import serialize_agent_card

        card_dict = card if isinstance(card, dict) else serialize_agent_card(card)
        display_name = card_dict.get("name", "agent")
        agent_id = agent_id or _slug(display_name)
        body = {
            "displayName": display_name,
            "description": card_dict.get("description", ""),
            "card": {"type": "A2A_AGENT_CARD", "content": card_dict},
        }
        # agentId is a query param on the create call; _make_request forwards
        # the query string embedded in the path for POST requests.
        return get_registry()._make_request(
            f"agents?agentId={agent_id}", method="POST", json_data=body
        )
    except Exception as exc:
        log.info("%s (registration failed: %s)", A2A_PREVIEW_SKIP, exc)
        return None


def get_a2a_agents(filter_str: str | None = None) -> list:
    """List registered A2A agents from Agent Registry (preview-optional).

    Returns the list of agent resource dicts, or ``[]`` (logged skip) if the
    preview surface / credentials are unavailable.
    """
    try:
        response = get_registry().list_agents(filter_str=filter_str)
        return response.get("agents", [])
    except Exception as exc:
        log.info("%s (discovery failed: %s)", A2A_PREVIEW_SKIP, exc)
        return []
