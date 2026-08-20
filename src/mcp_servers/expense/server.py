"""Expense MCP server — exposes expense submission, policy checks, and history over StreamableHTTP."""

import logging

logging.basicConfig(level=logging.INFO)
try:
    from otel_setup import setup_opentelemetry  # ty: ignore[unresolved-import]

    setup_opentelemetry("expense-mcp")
except Exception as e:
    logging.warning("OTel setup failed: %s", e)

from fastmcp import FastMCP

try:
    from .mock_db import check_policy as _check
    from .mock_db import get_expenses as _get
    from .mock_db import submit_expense as _submit
except ImportError:
    from mock_db import check_policy as _check  # ty: ignore[unresolved-import]
    from mock_db import get_expenses as _get  # ty: ignore[unresolved-import]
    from mock_db import submit_expense as _submit  # ty: ignore[unresolved-import]

mcp = FastMCP("expense-mcp", instructions="Submit and manage corporate expense reports.")


@mcp.tool()
def submit_expense(amount: float, category: str, description: str, user_id: str) -> dict:
    """Submit an expense report for reimbursement.

    Args:
        amount: Expense amount in USD
        category: Expense category (meals, transport, lodging, supplies, entertainment)
        description: Brief description of the expense
        user_id: Employee ID submitting the expense
    """
    return _submit(amount, category, description, user_id)


@mcp.tool()
def check_expense_policy(amount: float, category: str) -> dict:
    """Check if an expense amount is within corporate policy limits.

    Args:
        amount: Expense amount in USD
        category: Expense category (meals, transport, lodging, supplies, entertainment)
    """
    return _check(amount, category)


@mcp.tool()
def get_user_expenses(user_id: str, limit: int = 20) -> dict:
    """Get a user's most recent expenses, newest first.

    Returns ``total_count`` and ``total_amount`` over the user's ENTIRE history
    plus the most recent ``limit`` records under ``expenses``. When
    ``truncated`` is true, older records were omitted — say so rather than
    implying the listed records are the complete history.

    Args:
        user_id: Employee ID to look up expenses for
        limit: Maximum number of records to return (1-20, default 20)
    """
    return _get(user_id, limit)


if __name__ == "__main__":
    # stateless_http=True: any Cloud Run instance can serve any POST, so scaling
    # can't drop an MCP session mid-conversation ("Session terminated" 404).
    # See docs/notes/agent-registry-mcp-resolution.md.
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8003, stateless_http=True)
