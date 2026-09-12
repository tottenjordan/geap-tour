"""Tests for MCP server tools — mock data, tool logic, and the registration surface."""

from pathlib import Path

import pytest

from src.mcp_servers.booking import server as booking_server
from src.mcp_servers.booking.mock_db import (
    MAX_BOOKINGS_RETURNED,
    bookings,
    cancel_booking,
    create_booking,
    get_booking,
    list_bookings,
)
from src.mcp_servers.expense import server as expense_server
from src.mcp_servers.expense.mock_db import (
    MAX_EXPENSES_RETURNED,
    check_policy,
    expenses,
    get_expenses,
    submit_expense,
)
from src.mcp_servers.search import server as search_server
from src.mcp_servers.search.mock_db import FLIGHTS, HOTELS

MCP_SERVERS_DIR = Path(__file__).resolve().parents[1] / "src" / "mcp_servers"

# The annotation payload IAP must see, per tool. A table rather than a literal at
# the assert site because booking and expense mix read-only, additive and
# destructive tools, and they get the same treatment.
#
# Spelled out here independently of the `*_TOOL` constants the servers define: the
# expectation has to be able to disagree with the implementation, and those
# constants are duplicated per server module anyway (a shared module would not be
# in any server's Docker build context — see the comment in booking/server.py), so
# this table is also the drift guard across the three copies.
_READ_ONLY_HINTS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
# Creates a record and touches no existing one, so not destructive — but each call
# mints a fresh id (`BK-<uuid4>` / `EX-<uuid4>`) and nothing de-duplicates, so a
# replay books a second seat or files a second expense. That is what
# `idempotentHint: False` warns a caller, and any retry policy, about.
_ADDITIVE_HINTS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
# Mutates an existing record irreversibly (no tool un-cancels a booking), but
# converges: cancelling twice leaves the same booking cancelled.
_DESTRUCTIVE_HINTS = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": True,
    "openWorldHint": False,
}

EXPECTED_SEARCH_ANNOTATIONS = {
    "search_flights": _READ_ONLY_HINTS,
    "search_hotels": _READ_ONLY_HINTS,
}
EXPECTED_BOOKING_ANNOTATIONS = {
    "book_flight": _ADDITIVE_HINTS,
    "book_hotel": _ADDITIVE_HINTS,
    "cancel_booking": _DESTRUCTIVE_HINTS,
    "get_booking_details": _READ_ONLY_HINTS,
    "list_all_bookings": _READ_ONLY_HINTS,
}
EXPECTED_EXPENSE_ANNOTATIONS = {
    "submit_expense": _ADDITIVE_HINTS,
    "check_expense_policy": _READ_ONLY_HINTS,
    "get_user_expenses": _READ_ONLY_HINTS,
}

# Keyed by server directory name so `test_every_mcp_server_is_covered` can compare
# against what is actually on disk.
EXPECTED_ANNOTATIONS = {
    "search": (search_server, EXPECTED_SEARCH_ANNOTATIONS),
    "booking": (booking_server, EXPECTED_BOOKING_ANNOTATIONS),
    "expense": (expense_server, EXPECTED_EXPENSE_ANNOTATIONS),
}


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


class TestToolAnnotations:
    """IAP CEL conditions read these; absent hints make every condition misfire.

    `api.getAttribute('iap.googleapis.com/mcp.tool.isReadOnly', false)` returns the
    DEFAULT when the hint is absent, so `isReadOnly == true` would never match
    (denying a read-only tool) and `isDestructive == false` would always match
    (constraining nothing). The annotation is what makes the policy mean anything.

    Two properties, both about failures that are otherwise silent:

    1. The tool list is read back from the registry and pinned with `==`, not
       hardcoded at the assert site. A third search tool added later *with no
       annotations* is invisible to the rest of the repo — `_EXPECTED_MCP_TOOLS`
       in test_skill_definitions only forces someone to add its name — so it would
       ship and misfire the CEL. Here it fails.
    2. The whole serialized annotation dict is compared, not field-by-field
       attributes. `ToolAnnotations` is `extra="allow"`, so a misspelled
       `readonlyHint=True` is accepted and simply rides along as an extra key that
       IAP does not read; per-field asserts on the correctly-spelled names never
       see it, dict equality does. `model_dump(by_alias=True)` is also the JSON a
       client actually receives. (`to_mcp_tool()` itself is a pass-through — it
       hands the *same* `ToolAnnotations` object straight to `mcp.types.Tool` — so
       the dump, not the call, is what makes this a wire-form check.)

    Property 1 is per server, so it cannot see a *fourth* server appearing
    alongside the three; `test_every_mcp_server_is_covered` closes that.
    """

    @pytest.mark.parametrize("server_name", sorted(EXPECTED_ANNOTATIONS))
    async def test_every_registered_tool_serializes_its_expected_hints(self, server_name):
        server, expected = EXPECTED_ANNOTATIONS[server_name]
        tools = await server.mcp.list_tools()
        assert {t.name for t in tools} == set(expected)
        for tool in tools:
            dumped = tool.to_mcp_tool().model_dump(by_alias=True, exclude_none=True)
            assert dumped.get("annotations") == expected[tool.name], (
                f"{tool.name} does not serialize the expected annotations"
            )

    def test_every_mcp_server_is_covered(self):
        """A whole new server is the one way an unannotated tool still ships.

        The per-server check above only walks the registries it is handed, so a
        `src/mcp_servers/<new>/server.py` added later is simply not looked at —
        its tools would reach IAP with no hints and misfire the CEL exactly as
        the three here would have. Compare against the directory listing so
        adding a server forces adding its expectations.
        """
        on_disk = {path.parent.name for path in MCP_SERVERS_DIR.glob("*/server.py")}
        assert on_disk == set(EXPECTED_ANNOTATIONS)
