"""A2A agent card for the coordinator agent.

Pure functions that build an ``a2a.types.AgentCard`` describing the
coordinator's real abilities (flight/hotel search, booking, expense policy
checks, expense submission) so the agent is discoverable via A2A / Agent
Registry. No live GCP or MCP connections are made here.

Note on types: ``a2a-sdk`` 1.x models (``AgentCard``, ``AgentSkill``,
``AgentCapabilities``, ``AgentInterface``) are protobuf messages, not pydantic
models — so serialization goes through ``protobuf.json_format.MessageToDict``
(with a pydantic ``model_dump`` fallback for older a2a-sdk releases).
"""

import logging

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

from src.config import A2A_AGENT_NAME, A2A_AGENT_VERSION, coordinator_a2a_url

log = logging.getLogger(__name__)

COORDINATOR_DESCRIPTION = (
    "Corporate travel and expense coordinator. Searches and books flights and "
    "hotels, checks expense policy, and submits expenses for manager review."
)

# Skills mirror the coordinator's real, tool-backed capabilities (see
# src/agents/coordinator_agent.py Section 1). Kept as a module constant so tests
# can assert on the exact contract without constructing the card.
#
# Deliberately capability-level, not one-per-tool: `booking` covers book_flight
# and book_hotel. cancel_booking / get_booking_details / list_all_bookings are held
# by the coordinator and (since 2026-08-21) described by its instruction, but they
# stay off the card until an eval case exercises them — an A2A skill is a published
# contract, so advertise only what is tested. See
# docs/notes/prompt-architecture-audit.md.
_SKILLS: tuple[dict, ...] = (
    {
        "id": "flight_search",
        "name": "Flight Search",
        "description": "Search available flights between airports for given dates.",
        "tags": ["travel", "flights", "search"],
        "examples": ["Find flights from JFK to SFO next Monday"],
    },
    {
        "id": "hotel_search",
        "name": "Hotel Search",
        "description": "Search available hotels in a city for given dates.",
        "tags": ["travel", "hotels", "search"],
        "examples": ["Find a hotel in Chicago for two nights"],
    },
    {
        "id": "booking",
        "name": "Flight and Hotel Booking",
        "description": "Book a specific flight or hotel and return the confirmation.",
        "tags": ["travel", "booking"],
        "examples": ["Book flight FL001", "Book hotel HT042"],
    },
    {
        "id": "expense_policy_check",
        "name": "Expense Policy Check",
        "description": "Check an expense against corporate policy limits by category.",
        "tags": ["expense", "policy", "compliance"],
        "examples": ["Is a $90 dinner within the meals policy?"],
    },
    {
        "id": "expense_history",
        "name": "Expense History",
        "description": "Retrieve a user's recent expenses with amounts, categories and statuses.",
        "tags": ["expense", "history", "reporting"],
        "examples": ["Show my expenses", "What's the status of my last expense?"],
    },
    {
        "id": "expense_submission",
        "name": "Expense Submission",
        "description": (
            "Submit an expense for reimbursement, flagging over-limit items for manager review."
        ),
        "tags": ["expense", "submission"],
        "examples": ["Submit a $120 taxi expense"],
    },
)

SKILL_IDS: tuple[str, ...] = tuple(s["id"] for s in _SKILLS)


def _build_skills() -> list[AgentSkill]:
    return [
        AgentSkill(
            id=s["id"],
            name=s["name"],
            description=s["description"],
            tags=list(s["tags"]),
            examples=list(s["examples"]),
        )
        for s in _SKILLS
    ]


def build_agent_card(url: str | None = None) -> AgentCard:
    """Build the coordinator's A2A ``AgentCard``.

    Args:
      url: Optional A2A endpoint URL override. Defaults to
        ``config.coordinator_a2a_url()`` (the deployed Agent Engine endpoint).

    Returns:
      A fully-populated ``a2a.types.AgentCard``.
    """
    endpoint = url or coordinator_a2a_url()
    return AgentCard(
        name=A2A_AGENT_NAME,
        description=COORDINATOR_DESCRIPTION,
        version=A2A_AGENT_VERSION,
        capabilities=AgentCapabilities(streaming=True),
        skills=_build_skills(),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        supported_interfaces=[
            AgentInterface(url=endpoint, protocol_binding="JSONRPC"),
        ],
    )


def serialize_agent_card(card: AgentCard) -> dict:
    """Serialize an ``AgentCard`` to a plain dict, version-agnostically.

    a2a-sdk 1.x cards are protobuf (``MessageToDict``); older releases expose a
    pydantic ``model_dump``.
    """
    if hasattr(card, "model_dump"):
        return card.model_dump(mode="json", exclude_none=True)  # ty: ignore[call-non-callable]
    from google.protobuf.json_format import MessageToDict

    return MessageToDict(card)


def agent_card_dict(url: str | None = None) -> dict:
    """Return the coordinator agent card serialized to a dict."""
    return serialize_agent_card(build_agent_card(url))
