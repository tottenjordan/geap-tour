"""The search inventory's dates follow today, by the same rule the agent is given.

The inventory was pinned to June 2026. Once that month passed, an agent told
today's date resolved "June 15" to 2027-06-15, matched nothing, and reported no
flights. These tests pin the contract that prevents that: every flight date is the
next occurrence of its month/day on or after today, computed per call, and that is
the rule ``current_date_instruction`` states.
"""

from datetime import date

import pytest

from src.agents.current_date import current_date_instruction
from src.mcp_servers.search import mock_db
from src.mcp_servers.search import server as search_server


class TestNextOccurrence:
    def test_later_this_year_stays_this_year(self):
        assert mock_db.next_occurrence(6, 15, date(2026, 5, 1)) == date(2026, 6, 15)

    def test_today_counts_as_on_or_after(self):
        assert mock_db.next_occurrence(6, 15, date(2026, 6, 15)) == date(2026, 6, 15)

    def test_already_passed_rolls_to_next_year(self):
        assert mock_db.next_occurrence(6, 15, date(2026, 9, 28)) == date(2027, 6, 15)


# Spans before, inside and after the June window, plus a year boundary.
TODAYS = [date(2026, 5, 1), date(2026, 6, 17), date(2026, 9, 28), date(2027, 12, 31)]


@pytest.mark.parametrize("on", TODAYS, ids=str)
class TestTheInventoryFollowsToday:
    def test_no_flight_is_in_the_past(self, on):
        for f in mock_db.flights(on):
            assert date.fromisoformat(f["date"]) >= on, f

    def test_no_flight_is_more_than_a_year_out(self, on):
        for f in mock_db.flights(on):
            assert (date.fromisoformat(f["date"]) - on).days <= 366, f

    def test_hotel_window_covers_every_flight(self, on):
        """A hotel "available_to" before the trip reads as unbookable to the model."""
        latest = max(date.fromisoformat(f["date"]) for f in mock_db.flights(on))
        for h in mock_db.hotels(on):
            assert date.fromisoformat(h["available_from"]) == on
            assert date.fromisoformat(h["available_to"]) >= latest


def test_june_15_as_the_agent_resolves_it_finds_the_flights(monkeypatch):
    """The regression itself: on 2026-09-28 the agent searches 2027-06-15."""
    monkeypatch.setattr(mock_db, "today", lambda: date(2026, 9, 28))
    ids = {f["id"] for f in search_server.search_flights("SFO", "JFK", date="2027-06-15")}
    assert ids == {"FL001", "FL002"}
    assert search_server.search_flights("SFO", "JFK", date="2026-06-15") == []


def test_dates_are_computed_per_call_not_at_import(monkeypatch):
    """A Cloud Run instance lives for days; an import-time date would go stale."""
    monkeypatch.setattr(mock_db, "today", lambda: date(2026, 5, 1))
    before = search_server.search_flights("SFO", "JFK")[0]["date"]
    monkeypatch.setattr(mock_db, "today", lambda: date(2026, 9, 28))
    after = search_server.search_flights("SFO", "JFK")[0]["date"]
    assert (before, after) == ("2026-06-15", "2027-06-15")


def test_the_rule_matches_what_the_agent_is_told():
    """If the agent's rule for a yearless date changes, the inventory must follow."""
    assert "next occurrence on or after today" in current_date_instruction(date(2026, 9, 28))
