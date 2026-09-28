"""Mock flight and hotel data for the search MCP server.

**The dates are relative to today, not fixed.** The inventory was pinned to June
2026. Once that month passed, every agent that holds ``search_flights`` was told
today's date (``src/agents/current_date.py``) and the rule "a date without a year
means its next occurrence on or after today". So "June 15" became 2027-06-15, which
matched no flight, and the agent correctly reported that there were none. A fixed
year can only move that failure, never remove it.

So each flight keeps its month and day, which every eval prompt, trajectory and
traffic query quotes ("SFO to JFK on June 15"), and takes its year from the
**same rule the model is given**: the next occurrence on or after today. Hotel
availability is a rolling window starting today.

Materialised **per call**, never at import. A Cloud Run instance lives for days,
and a date computed once at import would go stale as silently as a fixed year.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

# Longer than a year, so the next occurrence of any date falls inside it.
HOTEL_WINDOW_DAYS = 400


def today() -> date:
    """UTC, matching ``src/agents/current_date.py``. That is the date the agent
    resolves "June 15" against. A different clock here would be off by one near
    midnight. It is duplicated rather than imported because this server's container
    holds only this directory."""
    return datetime.now(UTC).date()


def next_occurrence(month: int, day: int, on_or_after: date) -> date:
    """The first ``month``/``day`` on or after ``on_or_after``. This is the rule
    ``current_date_instruction`` gives the model for a date without a year."""
    candidate = date(on_or_after.year, month, day)
    return candidate if candidate >= on_or_after else date(on_or_after.year + 1, month, day)


def flights(on: date | None = None) -> list[dict]:
    """The flight inventory as of ``on`` (default: today, UTC)."""
    d = on or today()
    out = []
    for template in _FLIGHT_TEMPLATES:
        month, day = template["month_day"]
        when = next_occurrence(month, day, d).isoformat()
        # Rebuilt key by key so "date" keeps its place in the record the model reads.
        out.append({("date" if k == "month_day" else k): v for k, v in template.items()})
        out[-1]["date"] = when
    return out


def hotels(on: date | None = None) -> list[dict]:
    """The hotel inventory as of ``on`` (default: today, UTC)."""
    d = on or today()
    window = {
        "available_from": d.isoformat(),
        "available_to": (d + timedelta(days=HOTEL_WINDOW_DAYS)).isoformat(),
    }
    return [{**template, **window} for template in _HOTEL_TEMPLATES]


_FLIGHT_TEMPLATES: list[dict] = [
    {
        "id": "FL001",
        "airline": "United",
        "origin": "SFO",
        "destination": "JFK",
        "month_day": (6, 15),
        "price": 450.00,
        "departure": "08:00",
        "arrival": "16:30",
    },
    {
        "id": "FL002",
        "airline": "Delta",
        "origin": "SFO",
        "destination": "JFK",
        "month_day": (6, 15),
        "price": 520.00,
        "departure": "10:30",
        "arrival": "19:00",
    },
    {
        "id": "FL003",
        "airline": "American",
        "origin": "LAX",
        "destination": "ORD",
        "month_day": (6, 16),
        "price": 380.00,
        "departure": "07:00",
        "arrival": "13:15",
    },
    {
        "id": "FL004",
        "airline": "United",
        "origin": "ORD",
        "destination": "MIA",
        "month_day": (6, 17),
        "price": 290.00,
        "departure": "09:00",
        "arrival": "13:30",
    },
    {
        "id": "FL005",
        "airline": "Southwest",
        "origin": "SFO",
        "destination": "LAX",
        "month_day": (6, 15),
        "price": 150.00,
        "departure": "06:00",
        "arrival": "07:30",
    },
    {
        "id": "FL006",
        "airline": "Delta",
        "origin": "JFK",
        "destination": "LHR",
        "month_day": (6, 18),
        "price": 890.00,
        "departure": "20:00",
        "arrival": "08:00",
    },
    {
        "id": "FL007",
        "airline": "United",
        "origin": "SFO",
        "destination": "NRT",
        "month_day": (6, 20),
        "price": 1250.00,
        "departure": "11:00",
        "arrival": "15:00",
    },
]

_HOTEL_TEMPLATES: list[dict] = [
    {
        "id": "HT001",
        "name": "Grand Hyatt New York",
        "city": "New York",
        "price_per_night": 320.00,
        "rating": 4.5,
    },
    {
        "id": "HT002",
        "name": "The Palmer House",
        "city": "Chicago",
        "price_per_night": 250.00,
        "rating": 4.3,
    },
    {
        "id": "HT003",
        "name": "Fontainebleau Miami",
        "city": "Miami",
        "price_per_night": 400.00,
        "rating": 4.7,
    },
    {
        "id": "HT004",
        "name": "The Ritz-Carlton SF",
        "city": "San Francisco",
        "price_per_night": 550.00,
        "rating": 4.8,
    },
    {
        "id": "HT005",
        "name": "Budget Inn Downtown",
        "city": "New York",
        "price_per_night": 120.00,
        "rating": 3.2,
    },
    {
        "id": "HT006",
        "name": "Claridge's",
        "city": "London",
        "price_per_night": 680.00,
        "rating": 4.9,
    },
]
