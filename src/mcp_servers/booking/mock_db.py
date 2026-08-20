"""Mock booking database — in-memory store for flight and hotel reservations."""

import uuid
from datetime import datetime

bookings: dict[str, dict] = {}

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


def create_booking(booking_type: str, item_id: str, details: dict) -> dict:
    booking_id = f"BK-{uuid.uuid4().hex[:8].upper()}"
    booking = {
        "booking_id": booking_id,
        "type": booking_type,
        "item_id": item_id,
        "status": "confirmed",
        "created_at": datetime.now().isoformat(),
        **details,
    }
    bookings[booking_id] = booking
    return booking


def cancel_booking(booking_id: str) -> dict | None:
    if booking_id not in bookings:
        return None
    bookings[booking_id]["status"] = "cancelled"
    bookings[booking_id]["cancelled_at"] = datetime.now().isoformat()
    return bookings[booking_id]


def get_booking(booking_id: str) -> dict | None:
    return bookings.get(booking_id)


def list_bookings(limit: int = MAX_BOOKINGS_RETURNED) -> dict:
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
