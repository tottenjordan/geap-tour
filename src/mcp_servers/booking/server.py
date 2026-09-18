"""Booking MCP server — exposes flight and hotel booking tools over StreamableHTTP."""

import logging

logging.basicConfig(level=logging.INFO)
try:
    from otel_setup import setup_opentelemetry  # ty: ignore[unresolved-import]

    setup_opentelemetry("booking-mcp")
except Exception as e:
    logging.warning("OTel setup failed: %s", e)

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

try:
    from .mock_db import (
        BookingList,
        BookingRecord,
        ToolError,
        create_booking,
        get_booking,
        list_bookings,
    )
    from .mock_db import cancel_booking as _cancel
except ImportError:
    from mock_db import (  # ty: ignore[unresolved-import]  # ty: ignore[unresolved-import]
        BookingList,
        BookingRecord,
        ToolError,
        create_booking,
        get_booking,
        list_bookings,
    )
    from mock_db import cancel_booking as _cancel  # ty: ignore[unresolved-import]

mcp = FastMCP("booking-mcp", instructions="Book and manage flight and hotel reservations.")

# Declared so IAP's CEL conditions have attributes to read. Without them
# `api.getAttribute('iap.googleapis.com/mcp.tool.isDestructive', false)` falls back
# to its default, so the booking policy in scripts/setup_governance_policies.sh
# (`isDestructive == false`) would invert the moment it were bound: the hint would
# default to false, `false == false` would match, and cancel_booking — the one tool
# the clause exists to constrain — would go unconstrained. Nothing binds it today,
# so these hints are the prerequisite that makes the policy meaningful, not
# evidence that it is enforcing.
#
# Redefined here rather than shared with the other two servers: deploy_mcp_servers
# builds each server from its own directory (`--source src/mcp_servers/<name>`,
# Dockerfile `COPY . .`), so a src/mcp_servers/_annotations.py would be outside the
# build context and ImportError inside the container — a deploy-time failure, not a
# local one. The repo already answers this the same way (four copies of
# otel_setup.py); tests/test_mcp_servers.py carries the drift guard.

# The two lookups: they read the mock DB and nothing else.
READ_ONLY_TOOL = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
# create_booking mints a fresh `BK-<uuid4>` key per call, so it only ever adds —
# it cannot collide with, overwrite or remove an existing reservation, which is
# what keeps it non-destructive. It is *not* idempotent: nothing de-duplicates, so
# a retried call books a second seat rather than returning the first.
ADDITIVE_TOOL = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
# The one genuinely destructive tool on this server: it flips an existing booking
# to `cancelled` and no tool here undoes that. Still idempotent — a second cancel
# converges on the same cancelled booking (it only refreshes `cancelled_at`),
# so a retry after a dropped response is safe.
DESTRUCTIVE_TOOL = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False
)


@mcp.tool(annotations=ADDITIVE_TOOL)
def book_flight(flight_id: str, passenger_name: str) -> BookingRecord:
    """Book a flight for a passenger.

    Args:
        flight_id: The flight ID from search results (e.g., FL001)
        passenger_name: Full name of the passenger
    """
    return create_booking("flight", flight_id, {"passenger_name": passenger_name})


@mcp.tool(annotations=ADDITIVE_TOOL)
def book_hotel(hotel_id: str, guest_name: str, checkin: str, checkout: str) -> BookingRecord:
    """Book a hotel for a guest.

    Args:
        hotel_id: The hotel ID from search results (e.g., HT001)
        guest_name: Full name of the guest
        checkin: Check-in date (YYYY-MM-DD)
        checkout: Check-out date (YYYY-MM-DD)
    """
    return create_booking(
        "hotel",
        hotel_id,
        {
            "guest_name": guest_name,
            "checkin": checkin,
            "checkout": checkout,
        },
    )


@mcp.tool(annotations=DESTRUCTIVE_TOOL)
def cancel_booking(booking_id: str) -> BookingRecord | ToolError:
    """Cancel an existing booking.

    Args:
        booking_id: The booking ID to cancel (e.g., BK-A1B2C3D4)
    """
    result = _cancel(booking_id)
    if result is None:
        return {"error": f"Booking {booking_id} not found"}
    return result


@mcp.tool(annotations=READ_ONLY_TOOL)
def get_booking_details(booking_id: str) -> BookingRecord | ToolError:
    """Get details of an existing booking.

    Args:
        booking_id: The booking ID to look up
    """
    result = get_booking(booking_id)
    if result is None:
        return {"error": f"Booking {booking_id} not found"}
    return result


@mcp.tool(annotations=READ_ONLY_TOOL)
def list_all_bookings(limit: int = 20) -> BookingList:
    """List the most recent bookings in the system.

    Returns at most `limit` bookings (capped at 20), newest first, plus
    `total_count` over all stored bookings and a `truncated` flag. If
    `truncated` is true, say so — report the total count and make clear the
    listed bookings are only the most recent ones, never the complete list.

    Args:
        limit: Maximum number of bookings to return (default 20, capped at 20)
    """
    return list_bookings(limit)


if __name__ == "__main__":
    # stateless_http=True: any Cloud Run instance can serve any POST, so scaling
    # can't drop an MCP session mid-conversation ("Session terminated" 404).
    # See docs/notes/agent-registry-mcp-resolution.md.
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8002, stateless_http=True)
