"""Expense MCP server — exposes expense submission, policy checks, and history over StreamableHTTP."""

import logging

logging.basicConfig(level=logging.INFO)
try:
    from otel_setup import setup_opentelemetry  # ty: ignore[unresolved-import]

    setup_opentelemetry("expense-mcp")
except Exception as e:
    logging.warning("OTel setup failed: %s", e)

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

try:
    from .mock_db import ExpenseList, ExpenseRecord, PolicyCheck
    from .mock_db import check_policy as _check
    from .mock_db import get_expenses as _get
    from .mock_db import submit_expense as _submit
except ImportError:
    from mock_db import ExpenseList, ExpenseRecord, PolicyCheck  # ty: ignore[unresolved-import]
    from mock_db import check_policy as _check  # ty: ignore[unresolved-import]
    from mock_db import get_expenses as _get  # ty: ignore[unresolved-import]
    from mock_db import submit_expense as _submit  # ty: ignore[unresolved-import]

mcp = FastMCP("expense-mcp", instructions="Submit and manage corporate expense reports.")

# Declared so IAP's CEL conditions have attributes to read. The expense policy in
# scripts/setup_governance_policies.sh is currently a `mcp.toolName` allowlist and
# reads no annotation attribute, so these hints do not change it — but an absent
# hint is indistinguishable from `false` to `getAttribute(..., false)`, so any
# read-only or non-destructive clause added here later would silently invert
# (`isReadOnly == true` never matching, `isDestructive == false` always matching).
# Annotating every tool is what keeps that from being a trap; nothing binds any
# policy today, so this is the prerequisite, not enforcement.
#
# Redefined here rather than shared: each server is built from its own directory
# (`--source src/mcp_servers/<name>`, Dockerfile `COPY . .`), so a
# src/mcp_servers/_annotations.py would be outside the build context and
# ImportError inside the container. See the fuller note in booking/server.py.

# check_expense_policy is a pure function of POLICY_LIMITS; get_user_expenses only
# reads the store.
READ_ONLY_TOOL = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
# Writes under a fresh `EX-<uuid4>` key, so it adds without overwriting anything —
# non-destructive, but explicitly NOT idempotent: nothing de-duplicates, so a
# retried submission files a second claim for the same spend. The receipt-audit
# skill in src/skills/definitions.py documents that hazard for the model; this is
# the same fact stated where an authorization policy can read it.
ADDITIVE_TOOL = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)


@mcp.tool(annotations=ADDITIVE_TOOL)
def submit_expense(amount: float, category: str, description: str, user_id: str) -> ExpenseRecord:
    """Submit an expense report for reimbursement.

    Args:
        amount: Expense amount in USD
        category: Expense category (meals, transport, lodging, supplies, entertainment)
        description: Brief description of the expense
        user_id: Employee ID submitting the expense
    """
    return _submit(amount, category, description, user_id)


@mcp.tool(annotations=READ_ONLY_TOOL)
def check_expense_policy(amount: float, category: str) -> PolicyCheck:
    """Check if an expense amount is within corporate policy limits.

    Args:
        amount: Expense amount in USD
        category: Expense category (meals, transport, lodging, supplies, entertainment)
    """
    return _check(amount, category)


@mcp.tool(annotations=READ_ONLY_TOOL)
def get_user_expenses(user_id: str, limit: int = 20) -> ExpenseList:
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
