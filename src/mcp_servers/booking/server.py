"""Booking MCP server — exposes flight and hotel booking tools over StreamableHTTP."""

import logging

logging.basicConfig(level=logging.INFO)
try:
    from otel_setup import setup_opentelemetry  # ty: ignore[unresolved-import]

    setup_opentelemetry("booking-mcp")
except Exception as e:
    logging.warning("OTel setup failed: %s", e)

from fastmcp import FastMCP

try:
    from .mock_db import cancel_booking as _cancel
    from .mock_db import create_booking, get_booking, list_bookings
except ImportError:
    from mock_db import cancel_booking as _cancel  # ty: ignore[unresolved-import]
    from mock_db import create_booking, get_booking, list_bookings  # ty: ignore[unresolved-import]

mcp = FastMCP("booking-mcp", instructions="Book and manage flight and hotel reservations.")


@mcp.tool()
def book_flight(flight_id: str, passenger_name: str) -> dict:
    """Book a flight for a passenger.

    Args:
        flight_id: The flight ID from search results (e.g., FL001)
        passenger_name: Full name of the passenger
    """
    return create_booking("flight", flight_id, {"passenger_name": passenger_name})


@mcp.tool()
def book_hotel(hotel_id: str, guest_name: str, checkin: str, checkout: str) -> dict:
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


@mcp.tool()
def cancel_booking(booking_id: str) -> dict:
    """Cancel an existing booking.

    Args:
        booking_id: The booking ID to cancel (e.g., BK-A1B2C3D4)
    """
    result = _cancel(booking_id)
    if result is None:
        return {"error": f"Booking {booking_id} not found"}
    return result


@mcp.tool()
def get_booking_details(booking_id: str) -> dict:
    """Get details of an existing booking.

    Args:
        booking_id: The booking ID to look up
    """
    result = get_booking(booking_id)
    if result is None:
        return {"error": f"Booking {booking_id} not found"}
    return result


@mcp.tool()
def list_all_bookings(limit: int = 20) -> dict:
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
