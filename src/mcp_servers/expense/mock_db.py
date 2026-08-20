"""Mock expense database — in-memory store with corporate policy limit enforcement."""

import uuid
from datetime import datetime

POLICY_LIMITS = {
    "meals": 75.00,
    "transport": 200.00,
    "lodging": 400.00,
    "supplies": 100.00,
    "entertainment": 150.00,
}

expenses: dict[str, dict] = {}

# Hard cap on how many expense records :func:`get_expenses` hands back.
#
# ``expenses`` is an in-memory accumulator that every demo, eval and traffic run
# appends to and nothing ever prunes, so an uncapped "list all my expenses" grows
# without bound: EMP001 reached **96 records / 26KB of JSON** on a long-lived
# Cloud Run instance. A direct-tools agent has to absorb that payload *and*
# re-emit it, which pushed the router to ~17K input tokens per LLM hop (the
# coordinator, which delegates, sits at ~1.8K) and 14-21K-char answers. That
# token burn tripped the Vertex ``GenerateContent`` quota — 215 HTTP 429
# ``RESOURCE_EXHAUSTED`` responses in two hours, all attributed to the router
# engine — which surfaces to the caller as an empty-at-200 stream.
# See docs/notes/router-empty-responses-quota.md.
MAX_EXPENSES_RETURNED = 20


def submit_expense(amount: float, category: str, description: str, user_id: str) -> dict:
    expense_id = f"EX-{uuid.uuid4().hex[:8].upper()}"
    policy_check = check_policy(amount, category)
    expense = {
        "expense_id": expense_id,
        "amount": amount,
        "category": category,
        "description": description,
        "user_id": user_id,
        "status": "approved" if policy_check["within_policy"] else "pending_review",
        "policy_check": policy_check,
        "submitted_at": datetime.now().isoformat(),
    }
    expenses[expense_id] = expense
    return expense


def check_policy(amount: float, category: str) -> dict:
    category_lower = category.lower()
    if category_lower not in POLICY_LIMITS:
        return {
            "within_policy": False,
            "reason": f"Unknown category '{category}'. Valid: {', '.join(POLICY_LIMITS.keys())}",
        }
    limit = POLICY_LIMITS[category_lower]
    return {
        "within_policy": amount <= limit,
        "limit": limit,
        "amount": amount,
        "category": category_lower,
        "reason": None
        if amount <= limit
        else f"Amount ${amount:.2f} exceeds ${limit:.2f} limit for {category_lower}",
    }


def get_expenses(user_id: str, limit: int = MAX_EXPENSES_RETURNED) -> dict:
    """A user's most recent expenses, bounded and self-describing.

    Returns the newest ``limit`` records (clamped to ``MAX_EXPENSES_RETURNED``)
    alongside ``total_count``/``total_amount`` computed over **all** of the
    user's records, plus a ``truncated`` flag — so an agent can report the full
    picture honestly without the whole history entering its context.
    """
    matches = [e for e in expenses.values() if e["user_id"] == user_id]
    limit = max(1, min(int(limit), MAX_EXPENSES_RETURNED))
    recent = list(reversed(matches))[:limit]
    return {
        "user_id": user_id,
        "total_count": len(matches),
        "returned_count": len(recent),
        "truncated": len(recent) < len(matches),
        "total_amount": round(sum(e["amount"] for e in matches), 2),
        "expenses": recent,
    }
