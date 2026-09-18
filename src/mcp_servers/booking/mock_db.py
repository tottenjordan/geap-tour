"""Mock booking database — in-memory store for flight and hotel reservations."""

import uuid
from datetime import datetime
from typing import NotRequired, TypedDict


# These types live HERE, not in a shared src/mcp_servers/types.py, and that is a
# deployment constraint rather than a preference. Each server deploys with
# `gcloud run deploy --source src/mcp_servers/booking` over a `COPY . .`
# Dockerfile, so **only this directory ships**. An import reaching up to a parent
# package resolves fine in the dev venv and `ImportError`s in the container — the
# same trap the existing relative/absolute try-except below exists for.
#
# The expense server declares its own equivalents. That duplication is two
# independent deployables agreeing on a wire format, not one module copied; the
# agreement is enforced by tests/test_mcp_tool_types.py rather than by a shared
# base class neither container could import.
class ToolError(TypedDict):
    """What a tool returns instead of a record it could not find.

    Deliberately not a raised exception: an MCP tool's result goes to a model, and
    a raised error becomes a protocol failure rather than something the agent can
    relay to the user.
    """

    error: str


class BookingRecord(TypedDict):
    """A confirmed (or cancelled) reservation.

    The per-type fields are ``NotRequired`` because ``create_booking`` splats a
    caller-supplied ``details`` dict over the base record: a flight carries
    ``passenger_name``, a hotel ``guest_name``/``checkin``/``checkout``.
    ``cancelled_at`` appears only after a cancel.
    """

    booking_id: str
    type: str
    item_id: str
    status: str
    created_at: str
    cancelled_at: NotRequired[str]
    passenger_name: NotRequired[str]
    guest_name: NotRequired[str]
    checkin: NotRequired[str]
    checkout: NotRequired[str]


class BookingList(TypedDict):
    """The bounded-list contract: newest N records, plus what was left out.

    ``truncated`` is the load-bearing key and the reason this is a type at all.
    Its consumer is **a language model**, not Python — nothing reads
    ``result["truncated"]`` in code, the tool docstring tells the model to report
    truncation honestly. So the usual backstop of "a consumer would crash" does not
    exist here: a tool that quietly stops emitting the flag produces no error
    anywhere, just an agent confidently calling a partial list complete.
    """

    total_count: int
    returned_count: int
    truncated: bool
    bookings: list[BookingRecord]


bookings: dict[str, BookingRecord] = {}

# Hard ceiling on how many bookings ``list_bookings`` hands back in one call.
# ``bookings`` is an unbounded in-memory accumulator — every demo, eval, traffic
# run and bake-off appends to it and nothing ever prunes it, so it grows for the
# life of a Cloud Run instance. The same shape in the expense server reached 96
# records / 26KB of JSON, and a direct-tools agent (which the coordinator is for
# this toolset) has to absorb that payload *and* re-emit it — the token blow-up
# that tripped the Vertex ``GenerateContent`` quota and turned the router's
# answers into empty-at-200 streams.
# See docs/notes/router-empty-responses-quota.md.
MAX_BOOKINGS_RETURNED = 20


def create_booking(booking_type: str, item_id: str, details: dict) -> BookingRecord:
    booking_id = f"BK-{uuid.uuid4().hex[:8].upper()}"
    booking: BookingRecord = {
        "booking_id": booking_id,
        "type": booking_type,
        "item_id": item_id,
        "status": "confirmed",
        "created_at": datetime.now().isoformat(),
        **details,
    }
    bookings[booking_id] = booking
    return booking


def cancel_booking(booking_id: str) -> BookingRecord | None:
    if booking_id not in bookings:
        return None
    bookings[booking_id]["status"] = "cancelled"
    bookings[booking_id]["cancelled_at"] = datetime.now().isoformat()
    return bookings[booking_id]


def get_booking(booking_id: str) -> BookingRecord | None:
    return bookings.get(booking_id)


def list_bookings(limit: int = MAX_BOOKINGS_RETURNED) -> BookingList:
    """The most recent bookings, bounded and self-describing.

    Returns the newest ``limit`` records (clamped to ``MAX_BOOKINGS_RETURNED``)
    alongside a ``total_count`` computed over **all** stored bookings and a
    ``truncated`` flag — so an agent can report the full picture honestly
    without the whole history entering its context.
    """
    all_bookings = list(bookings.values())
    limit = max(1, min(int(limit), MAX_BOOKINGS_RETURNED))
    recent = list(reversed(all_bookings))[:limit]
    return {
        "total_count": len(all_bookings),
        "returned_count": len(recent),
        "truncated": len(recent) < len(all_bookings),
        "bookings": recent,
    }
