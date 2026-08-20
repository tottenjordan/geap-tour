"""Tests for MCP server tools — validates mock data and tool logic."""

from src.mcp_servers.booking.mock_db import (
    MAX_BOOKINGS_RETURNED,
    bookings,
    cancel_booking,
    create_booking,
    get_booking,
    list_bookings,
)
from src.mcp_servers.expense.mock_db import (
    MAX_EXPENSES_RETURNED,
    check_policy,
    expenses,
    get_expenses,
    submit_expense,
)
from src.mcp_servers.search.mock_db import FLIGHTS, HOTELS


class TestSearchMockDB:
    def test_flights_have_required_fields(self):
        for f in FLIGHTS:
            assert "id" in f
            assert "origin" in f
            assert "destination" in f
            assert "price" in f

    def test_hotels_have_required_fields(self):
        for h in HOTELS:
            assert "id" in h
            assert "city" in h
            assert "price_per_night" in h

    def test_flights_not_empty(self):
        assert len(FLIGHTS) > 0

    def test_hotels_not_empty(self):
        assert len(HOTELS) > 0


class TestBookingMockDB:
    def setup_method(self):
        bookings.clear()

    def test_create_booking(self):
        result = create_booking("flight", "FL001", {"passenger_name": "Test User"})
        assert result["booking_id"].startswith("BK-")
        assert result["status"] == "confirmed"
        assert result["type"] == "flight"

    def test_cancel_booking(self):
        result = create_booking("hotel", "HT001", {"guest_name": "Test"})
        cancelled = cancel_booking(result["booking_id"])
        assert cancelled["status"] == "cancelled"

    def test_cancel_nonexistent(self):
        assert cancel_booking("BK-NONEXISTENT") is None

    def test_get_booking(self):
        result = create_booking("flight", "FL001", {"passenger_name": "Test"})
        found = get_booking(result["booking_id"])
        assert found is not None
        assert found["booking_id"] == result["booking_id"]

    def test_list_bookings(self):
        create_booking("flight", "FL001", {"passenger_name": "A"})
        create_booking("hotel", "HT001", {"guest_name": "B"})
        result = list_bookings()
        assert result["total_count"] == 2
        assert result["returned_count"] == 2
        assert result["truncated"] is False
        assert len(result["bookings"]) == 2

    def test_list_bookings_is_capped_and_newest_first(self):
        """``bookings`` is the same unbounded accumulator ``expenses`` was.

        Nothing prunes it, so every demo, eval and traffic run grows it on a
        long-lived Cloud Run instance. The coordinator holds the booking toolset
        directly, so an uncapped "list all bookings" is absorbed *and* re-emitted
        on its context — the exact token blow-up behind the router's HTTP 429
        empty responses (docs/notes/router-empty-responses-quota.md).
        """
        for i in range(MAX_BOOKINGS_RETURNED + 5):
            create_booking("flight", f"FL{i:03d}", {"passenger_name": "A"})
        result = list_bookings()
        assert result["total_count"] == MAX_BOOKINGS_RETURNED + 5
        assert result["returned_count"] == MAX_BOOKINGS_RETURNED
        assert result["truncated"] is True
        # Newest first, so the most recent booking leads.
        assert result["bookings"][0]["item_id"] == f"FL{MAX_BOOKINGS_RETURNED + 4:03d}"
        assert len(result["bookings"]) == MAX_BOOKINGS_RETURNED

    def test_list_bookings_limit_cannot_exceed_the_cap(self):
        """A model asking for more than the cap must not reopen the blow-up."""
        for i in range(MAX_BOOKINGS_RETURNED + 5):
            create_booking("flight", f"FL{i:03d}", {"passenger_name": "A"})
        assert list_bookings(limit=1000)["returned_count"] == MAX_BOOKINGS_RETURNED
        assert list_bookings(limit=0)["returned_count"] == 1
        assert list_bookings(limit=3)["returned_count"] == 3

    def test_list_bookings_on_an_empty_store_is_well_formed(self):
        result = list_bookings()
        assert result["bookings"] == []
        assert result["total_count"] == 0
        assert result["returned_count"] == 0
        assert result["truncated"] is False


class TestExpenseMockDB:
    def setup_method(self):
        expenses.clear()

    def test_check_policy_within_limit(self):
        result = check_policy(50.0, "meals")
        assert result["within_policy"] is True

    def test_check_policy_over_limit(self):
        result = check_policy(100.0, "meals")
        assert result["within_policy"] is False
        assert result["reason"] is not None

    def test_check_policy_unknown_category(self):
        result = check_policy(10.0, "unknown")
        assert result["within_policy"] is False

    def test_submit_expense_within_policy(self):
        result = submit_expense(50.0, "meals", "lunch", "EMP001")
        assert result["status"] == "approved"

    def test_submit_expense_over_policy(self):
        result = submit_expense(500.0, "meals", "fancy dinner", "EMP001")
        assert result["status"] == "pending_review"

    def test_get_expenses_by_user(self):
        submit_expense(50.0, "meals", "lunch", "EMP001")
        submit_expense(30.0, "transport", "taxi", "EMP002")
        assert len(get_expenses("EMP001")["expenses"]) == 1
        assert len(get_expenses("EMP002")["expenses"]) == 1

    def test_get_expenses_reports_totals_over_all_records(self):
        submit_expense(50.0, "meals", "lunch", "EMP001")
        submit_expense(25.0, "meals", "coffee", "EMP001")
        result = get_expenses("EMP001")
        assert result["user_id"] == "EMP001"
        assert result["total_count"] == 2
        assert result["returned_count"] == 2
        assert result["total_amount"] == 75.0
        assert result["truncated"] is False

    def test_get_expenses_is_capped_and_newest_first(self):
        """The store is an unbounded accumulator; the tool payload must not be.

        An uncapped "list all expenses" grew to 96 records / 26KB of JSON for
        EMP001, which a direct-tools agent has to absorb *and* re-emit — the
        token blow-up behind the router's HTTP 429 empty responses.
        """
        for i in range(MAX_EXPENSES_RETURNED + 5):
            submit_expense(10.0, "meals", f"lunch {i}", "EMP001")
        result = get_expenses("EMP001")
        assert result["total_count"] == MAX_EXPENSES_RETURNED + 5
        assert result["returned_count"] == MAX_EXPENSES_RETURNED
        assert result["truncated"] is True
        # Newest first, so the most recent submission leads.
        descriptions = [e["description"] for e in result["expenses"]]
        assert descriptions[0] == f"lunch {MAX_EXPENSES_RETURNED + 4}"
        assert len(descriptions) == MAX_EXPENSES_RETURNED

    def test_get_expenses_limit_cannot_exceed_the_cap(self):
        """A model asking for more than the cap must not reopen the blow-up."""
        for i in range(MAX_EXPENSES_RETURNED + 5):
            submit_expense(10.0, "meals", f"lunch {i}", "EMP001")
        assert get_expenses("EMP001", limit=1000)["returned_count"] == MAX_EXPENSES_RETURNED
        assert get_expenses("EMP001", limit=0)["returned_count"] == 1
        assert get_expenses("EMP001", limit=3)["returned_count"] == 3

    def test_get_expenses_for_unknown_user_is_empty_but_well_formed(self):
        result = get_expenses("NOBODY")
        assert result["expenses"] == []
        assert result["total_count"] == 0
        assert result["total_amount"] == 0
        assert result["truncated"] is False
