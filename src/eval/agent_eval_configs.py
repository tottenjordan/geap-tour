"""Per-agent evaluation configs — test cases, AgentInfo builders, and metric selectors."""

from agentplatform import types

from src.eval.batch_eval import (
    ALL_MCP_TOOL_NAMES,
    BOOKING_TOOL_NAMES,
    EXPENSE_TOOL_NAMES,
    SEARCH_TOOL_NAMES,
    declared_tools,
)
from src.eval.batch_eval import EVAL_CASES as COORDINATOR_EVAL_CASES

# ---------------------------------------------------------------------------
# Travel agent test cases
# ---------------------------------------------------------------------------
TRAVEL_EVAL_CASES = [
    {
        "prompt": "Find flights from SFO to JFK on June 15",
        "reference": "Flights from SFO to JFK: United FL001 at $450 departing 08:00, Delta FL002 at $520 departing 10:30.",
        "category": "flight_search",
        "expected_tool": "search_mcp_search_flights",
        "expected_signals": ["SFO", "JFK", "FL001", "FL002"],
        "description": "Basic flight search with known routes",
    },
    {
        "prompt": "Search for flights from LAX to Chicago on June 16",
        "reference": "American Airlines flight FL003 from LAX to ORD at $380, departing 07:00.",
        "category": "flight_search",
        "expected_tool": "search_mcp_search_flights",
        "expected_signals": ["LAX", "ORD", "FL003"],
        "description": "Flight search with city name mapping",
    },
    {
        "prompt": "Are there any flights from SFO to Los Angeles on June 15?",
        "reference": "Southwest flight FL005 from SFO to LAX at $150, departing 06:00.",
        "category": "flight_search",
        "expected_tool": "search_mcp_search_flights",
        "expected_signals": ["SFO", "LAX", "FL005"],
        "description": "Short-haul domestic flight search",
    },
    {
        "prompt": "Search for hotels in New York under $350 per night",
        "reference": "Grand Hyatt New York at $320/night (4.5 rating) and Budget Inn Downtown at $120/night (3.2 rating).",
        "category": "hotel_search",
        "expected_tool": "search_mcp_search_hotels",
        "expected_signals": ["Grand Hyatt", "Budget Inn"],
        "description": "Hotel search with price filter",
    },
    {
        "prompt": "Find me a hotel in Miami",
        "reference": "Fontainebleau Miami at $400/night with a 4.7 rating.",
        "category": "hotel_search",
        "expected_tool": "search_mcp_search_hotels",
        "expected_signals": ["Fontainebleau", "Miami"],
        "description": "Hotel search without price constraint",
    },
    {
        "prompt": "Book flight FL001 for Alice Johnson",
        "reference": "Flight FL001 (United, SFO to JFK) booked and confirmed for Alice Johnson.",
        "category": "booking",
        "expected_tool": "booking_mcp_book_flight",
        "expected_signals": ["FL001", "Alice Johnson", "confirmed"],
        "description": "Flight booking with valid flight ID",
    },
    {
        "prompt": "Book hotel HT002 for Bob Smith, checkin June 15, checkout June 18",
        "reference": "Hotel HT002 booked for Bob Smith, check-in June 15, check-out June 18. Confirmation provided.",
        "category": "booking",
        "expected_tool": "booking_mcp_book_hotel",
        "expected_signals": ["HT002", "Bob Smith"],
        "description": "Hotel booking with dates",
    },
    {
        "prompt": "Find flights from XYZ to ABC tomorrow",
        "reference": "No flights found for the route XYZ to ABC. These may be invalid airport codes. Please provide valid IATA airport codes.",
        "category": "edge_case",
        "expected_tool": "search_mcp_search_flights",
        "expected_signals": [],
        "description": "Invalid airport codes — should handle gracefully",
    },
    {
        "prompt": "Search hotels in Atlantis under $100",
        "reference": "No hotels found in Atlantis. This location may not be in our database. Please try a different city.",
        "category": "edge_case",
        "expected_tool": "search_mcp_search_hotels",
        "expected_signals": [],
        "description": "Non-existent city — should handle gracefully",
    },
    {
        "prompt": "What are the cheapest flight options from SFO to anywhere on the East Coast?",
        "reference": "Cheapest flights from SFO: FL001 to JFK at $450 (United), FL005 to LAX at $150 (Southwest). Search results listed by price.",
        "category": "flight_search",
        "expected_tool": "search_mcp_search_flights",
        "expected_signals": ["SFO"],
        "description": "Open-ended destination search",
    },
]


# ---------------------------------------------------------------------------
# Expense agent test cases
# ---------------------------------------------------------------------------
EXPENSE_EVAL_CASES = [
    {
        "prompt": "Check if a $50 meal expense is within policy",
        "reference": "A $50 meal expense is within the corporate policy limit of $75 for meals.",
        "category": "policy_check",
        "expected_tool": "expense_mcp_check_expense_policy",
        "expected_signals": ["within", "75"],
        "description": "Meal under $75 limit — should approve",
    },
    {
        "prompt": "Is a $180 transport expense within corporate policy?",
        "reference": "A $180 transport expense is within the corporate policy limit of $200 for transportation.",
        "category": "policy_check",
        "expected_tool": "expense_mcp_check_expense_policy",
        "expected_signals": ["within", "200"],
        "description": "Transport under $200 limit — should approve",
    },
    {
        "prompt": "Check policy for a $500 entertainment expense",
        "reference": "A $500 entertainment expense exceeds the corporate policy limit of $150. This requires manager review.",
        "category": "policy_over_limit",
        "expected_tool": "expense_mcp_check_expense_policy",
        "expected_signals": ["exceeds", "150", "entertainment"],
        "description": "Entertainment over $150 limit — should flag",
    },
    {
        "prompt": "Is a $100 meal expense allowed?",
        "reference": "A $100 meal expense exceeds the corporate policy limit of $75 for meals. This requires manager review.",
        "category": "policy_over_limit",
        "expected_tool": "expense_mcp_check_expense_policy",
        "expected_signals": ["exceeds", "75", "meal"],
        "description": "Meal over $75 limit — should flag",
    },
    {
        "prompt": "Submit a $45 meals expense for lunch meeting, user ID EMP001",
        "reference": "Expense submitted: $45 meals expense for EMP001. Status: approved (within $75 policy limit).",
        "category": "submission",
        "expected_tool": "expense_mcp_submit_expense",
        "expected_signals": ["EMP001", "45", "approved"],
        "description": "Within-policy submission — should auto-approve",
    },
    {
        "prompt": "Submit a $500 entertainment expense for team event, user ID EMP002",
        "reference": "A $500 entertainment expense exceeds the $150 policy limit. Status: pending_review, requires manager approval.",
        "category": "submission_over",
        "expected_tool": "expense_mcp_submit_expense",
        "expected_signals": ["EMP002", "pending_review", "exceeds"],
        "description": "Over-limit submission — should flag pending_review",
    },
    {
        "prompt": "Show all expenses for user EMP001",
        "reference": "Expense history for EMP001 retrieved, showing all submitted expenses with amounts, categories, and statuses.",
        "category": "history",
        "expected_tool": "expense_mcp_get_user_expenses",
        "expected_signals": ["EMP001"],
        "description": "Expense history retrieval",
    },
    {
        "prompt": "What's the corporate limit for lodging expenses?",
        "reference": "The corporate policy limit for lodging expenses is $400 per night.",
        "category": "policy_inquiry",
        "expected_tool": "expense_mcp_check_expense_policy",
        "expected_signals": ["400", "lodging"],
        "description": "Direct policy limit inquiry",
    },
    {
        "prompt": "Check policy for $1000 in the 'unknown' category",
        "reference": "The category 'unknown' is not a valid expense category. Valid categories are: meals, transport, lodging, supplies, entertainment.",
        "category": "invalid_category",
        "expected_tool": "expense_mcp_check_expense_policy",
        "expected_signals": ["unknown"],
        "description": "Invalid expense category — should return helpful error",
    },
    {
        "prompt": "Submit a $90 supplies expense for office materials, user ID EMP003",
        "reference": "Expense submitted: $90 supplies expense for EMP003. Status: approved (within $100 policy limit).",
        "category": "submission",
        "expected_tool": "expense_mcp_submit_expense",
        "expected_signals": ["EMP003", "90", "supplies"],
        "description": "Supplies within $100 limit — should approve",
    },
]


# ---------------------------------------------------------------------------
# Router agent test cases (with expected complexity levels)
# ---------------------------------------------------------------------------
ROUTER_EVAL_CASES = [
    {
        "prompt": "Find flights from SFO to JFK",
        "reference": "Flights from SFO to JFK: United FL001 at $450, Delta FL002 at $520.",
        "category": "low_complexity",
        "expected_tool": "search_mcp_search_flights",
        "expected_signals": ["SFO", "JFK"],
        "expected_complexity": "low",
        "description": "Simple single-intent flight search",
    },
    {
        "prompt": "What's the expense policy for meals?",
        "reference": "The corporate policy limit for meals is $75. Amounts above this require manager review.",
        "category": "low_complexity",
        "expected_tool": "expense_mcp_check_expense_policy",
        "expected_signals": ["75", "meal"],
        "expected_complexity": "low",
        "description": "Simple policy lookup",
    },
    {
        "prompt": "Search hotels in Chicago under $200",
        "reference": "Hotels in Chicago under $200/night listed with names, prices, and ratings.",
        "category": "low_complexity",
        "expected_tool": "search_mcp_search_hotels",
        "expected_signals": ["Chicago"],
        "expected_complexity": "low",
        "description": "Simple hotel search with filter",
    },
    {
        "prompt": "Check if a $50 transport expense is within policy",
        "reference": "A $50 transport expense is within the corporate policy limit of $200.",
        "category": "low_complexity",
        "expected_tool": "expense_mcp_check_expense_policy",
        "expected_signals": ["within", "200"],
        "expected_complexity": "low",
        "description": "Simple policy check",
    },
    {
        "prompt": "Find flights to NYC and compare the cheapest options by airline",
        "reference": "Flight options to NYC compared by airline and price, with the cheapest option highlighted.",
        "category": "medium_complexity",
        "expected_tool": "search_mcp_search_flights",
        "expected_signals": ["NYC"],
        "expected_complexity": "medium",
        "description": "Comparison requiring moderate reasoning",
    },
    {
        "prompt": "Search hotels in Boston, then check if the nightly rate fits our lodging policy",
        "reference": "Hotels in Boston listed with rates. The lodging policy limit is $400/night. Hotels under $400 are within policy.",
        "category": "medium_complexity",
        "expected_tool": "search_mcp_search_hotels",
        "expected_signals": ["Boston", "400"],
        "expected_complexity": "medium",
        "description": "Two-step: search + policy check",
    },
    {
        "prompt": "Show my expense history and flag any items that exceeded policy limits",
        "reference": "Expense history retrieved. Items exceeding policy limits flagged with the applicable limit and overage amount.",
        "category": "medium_complexity",
        "expected_tool": "expense_mcp_get_user_expenses",
        "expected_signals": [],
        "expected_complexity": "medium",
        "description": "History retrieval with analysis",
    },
    {
        "prompt": (
            "Plan a 5-day trip to Tokyo for a team of 4: find flights, hotels near "
            "Shibuya, estimate daily meal expenses, and check what our corporate policy "
            "allows for international entertainment expenses."
        ),
        "reference": "Trip plan for Tokyo: flights from SFO, hotel options near Shibuya, estimated daily meal costs within $75/person policy, entertainment policy limit of $150.",
        "category": "high_complexity",
        "expected_tool": "multiple",
        "expected_signals": ["Tokyo"],
        "expected_complexity": "high",
        "description": "Multi-step cross-domain planning",
    },
    {
        "prompt": (
            "Compare individual vs group flight bookings for our team retreat to Denver. "
            "Factor in cancellation policies, per-diem meal expenses, and whether hotels "
            "near the conference center or downtown with transport are more cost-effective."
        ),
        "reference": "Comparison of individual vs group bookings to Denver with cost breakdown, per-diem meal estimates within policy, and hotel location cost-effectiveness analysis.",
        "category": "high_complexity",
        "expected_tool": "multiple",
        "expected_signals": ["Denver"],
        "expected_complexity": "high",
        "description": "Complex multi-factor comparison",
    },
    {
        "prompt": (
            "Analyze EMP001's expense history: they overspent on entertainment last quarter. "
            "Draft a policy recommendation for new entertainment limits, and submit my "
            "$45 lunch receipt while you're at it."
        ),
        "reference": "EMP001 expense analysis showing entertainment overspend. Policy recommendation drafted. $45 lunch expense submitted for EMP001, status: approved.",
        "category": "high_complexity",
        "expected_tool": "multiple",
        "expected_signals": ["EMP001", "entertainment"],
        "expected_complexity": "high",
        "description": "Analysis + action + submission",
    },
    {
        "prompt": (
            "Book the cheapest SFO-JFK flight, find a hotel within walking distance of "
            "350 5th Ave, cross-reference hotel ratings, check our lodging policy limit, "
            "and submit a pre-approval expense for the estimated total trip cost."
        ),
        "reference": "Cheapest SFO-JFK flight booked. Hotels near 350 5th Ave listed with ratings. Lodging policy limit is $400/night. Pre-approval expense submitted with estimated total trip cost.",
        "category": "high_complexity",
        "expected_tool": "multiple",
        "expected_signals": ["SFO", "JFK", "400"],
        "expected_complexity": "high",
        "description": "Multi-step booking + policy + expense pipeline",
    },
    {
        "prompt": "How much can I spend on meals per day while traveling?",
        "reference": "The corporate meal policy limit is $75 per day while traveling.",
        "category": "low_complexity",
        "expected_tool": "expense_mcp_check_expense_policy",
        "expected_signals": ["75", "meal"],
        "expected_complexity": "low",
        "description": "Simple policy inquiry phrased as question",
    },
    # ── Grown 2026-08-22: 12 -> 40 cases ──────────────────────────────────
    # At n=12 the 80% `routing_accuracy_pct` alert was unresolvable: the Wilson
    # interval spanned 80% for EVERY possible outcome, including a perfect 12/12,
    # so a healthy router could not be distinguished from a failing one and one
    # case flipping moved the metric 8.3 points. `stats.min_n_for_threshold` puts
    # the requirement at ~40 for a healthy rate. Balanced across the three bands so
    # accuracy is not dominated by whichever band happens to be easiest.
    # ── low: single intent, one tool, no comparison or synthesis ───────────
    {
        "prompt": "Search flights from LAX to ORD",
        "reference": "American FL003 from LAX to ORD at $380, departing 07:00.",
        "category": "low_complexity",
        "expected_tool": "search_mcp_search_flights",
        "expected_signals": ["LAX", "ORD"],
        "expected_complexity": "low",
        "description": "Single-intent flight search, explicit airport codes",
    },
    {
        "prompt": "What is the lodging limit?",
        "reference": "The lodging limit is $400 per night.",
        "category": "low_complexity",
        "expected_tool": "expense_mcp_check_expense_policy",
        "expected_signals": ["400"],
        "expected_complexity": "low",
        "description": "Flat policy lookup, no amount to evaluate",
    },
    {
        "prompt": "Find me a hotel in Miami",
        "reference": "Fontainebleau Miami at $400/night, 4.7 rating.",
        "category": "low_complexity",
        "expected_tool": "search_mcp_search_hotels",
        "expected_signals": ["Miami"],
        "expected_complexity": "low",
        "description": "Single-intent hotel search, no constraints",
    },
    {
        "prompt": "Is a $30 taxi within policy?",
        "reference": "A $30 transport expense is within the $200 transport limit.",
        "category": "low_complexity",
        "expected_tool": "expense_mcp_check_expense_policy",
        "expected_signals": ["within", "200"],
        "expected_complexity": "low",
        "description": "Single policy check, clearly under the limit",
    },
    {
        "prompt": "Show my recent expenses",
        "reference": "Your most recent expenses, newest first.",
        "category": "low_complexity",
        "expected_tool": "expense_mcp_get_user_expenses",
        "expected_signals": [],
        "expected_complexity": "low",
        "description": "Plain history retrieval",
    },
    {
        "prompt": "Cancel booking BK001",
        "reference": "Booking BK001 has been cancelled.",
        "category": "low_complexity",
        "expected_tool": "booking_mcp_cancel_booking",
        "expected_signals": ["BK001"],
        "expected_complexity": "low",
        "description": "Single mutation with an explicit id",
    },
    {
        "prompt": "What is the entertainment limit?",
        "reference": "The entertainment limit is $150.",
        "category": "low_complexity",
        "expected_tool": "expense_mcp_check_expense_policy",
        "expected_signals": ["150"],
        "expected_complexity": "low",
        "description": "Flat policy lookup for a second category",
    },
    {
        "prompt": "Get the details for booking BK002",
        "reference": "Details for booking BK002.",
        "category": "low_complexity",
        "expected_tool": "booking_mcp_get_booking_details",
        "expected_signals": ["BK002"],
        "expected_complexity": "low",
        "description": "Single lookup by id",
    },
    {
        "prompt": "Are there flights from SFO to LAX?",
        "reference": "Southwest FL005 from SFO to LAX at $150.",
        "category": "low_complexity",
        "expected_tool": "search_mcp_search_flights",
        "expected_signals": ["SFO", "LAX"],
        "expected_complexity": "low",
        "description": "Short-haul single-intent search",
    },
    # ── medium: two steps, or one step plus a judgement ────────────────────
    {
        "prompt": "Find hotels in Chicago and tell me which ones fit the lodging policy",
        "reference": "Chicago hotels with each nightly rate checked against the $400 lodging limit.",
        "category": "medium_complexity",
        "expected_tool": "search_mcp_search_hotels",
        "expected_signals": ["Chicago"],
        "expected_complexity": "medium",
        "description": "Search plus a policy judgement over the results",
    },
    {
        "prompt": "Submit a $95 meal expense for EMP001 and tell me if it needs review",
        "reference": "The $95 meal exceeds the $75 limit; submitted and flagged for manager review.",
        "category": "medium_complexity",
        "expected_tool": "expense_mcp_submit_expense",
        "expected_signals": ["75", "review"],
        "expected_complexity": "medium",
        "description": "Policy check then submit, with an over-limit consequence",
    },
    {
        "prompt": "Compare the SFO-JFK flights and tell me which is better value",
        "reference": "FL001 at $450 vs FL002 at $520, with a value recommendation.",
        "category": "medium_complexity",
        "expected_tool": "search_mcp_search_flights",
        "expected_signals": ["FL001", "FL002"],
        "expected_complexity": "medium",
        "description": "Search plus comparative reasoning",
    },
    {
        "prompt": "Which of my recent expenses were over their category limits?",
        "reference": "Recent expenses with each checked against its category limit.",
        "category": "medium_complexity",
        "expected_tool": "expense_mcp_get_user_expenses",
        "expected_signals": [],
        "expected_complexity": "medium",
        "description": "Retrieval plus per-item policy evaluation",
    },
    {
        "prompt": "Book flight FL001 for Alice Johnson and confirm the fare is reasonable",
        "reference": "FL001 booked for Alice Johnson at $450, with a note on the fare.",
        "category": "medium_complexity",
        "expected_tool": "booking_mcp_book_flight",
        "expected_signals": ["FL001", "Alice Johnson"],
        "expected_complexity": "medium",
        "description": "Mutation plus a judgement on the result",
    },
    {
        "prompt": "Find a hotel in New York under $350 and check it against the lodging limit",
        "reference": "New York hotels under $350, all within the $400 lodging limit.",
        "category": "medium_complexity",
        "expected_tool": "search_mcp_search_hotels",
        "expected_signals": ["New York"],
        "expected_complexity": "medium",
        "description": "Constrained search plus a policy cross-check",
    },
    {
        "prompt": "List all bookings and tell me which are still upcoming",
        "reference": "Recent bookings with upcoming ones identified.",
        "category": "medium_complexity",
        "expected_tool": "booking_mcp_list_all_bookings",
        "expected_signals": [],
        "expected_complexity": "medium",
        "description": "Retrieval plus filtering judgement",
    },
    {
        "prompt": "I spent $220 on a taxi — can I claim it, and what happens if not?",
        "reference": "$220 exceeds the $200 transport limit; it is submitted and flagged for review.",
        "category": "medium_complexity",
        "expected_tool": "expense_mcp_check_expense_policy",
        "expected_signals": ["200", "review"],
        "expected_complexity": "medium",
        "description": "Policy check plus consequence explanation",
    },
    {
        "prompt": "Search flights to Chicago and hotels there for the same trip",
        "reference": "Chicago flight and hotel options for one trip.",
        "category": "medium_complexity",
        "expected_tool": "search_mcp_search_flights",
        "expected_signals": ["Chicago"],
        "expected_complexity": "medium",
        "description": "Two coordinated searches, no synthesis beyond pairing",
    },
    {
        "prompt": "Check a $160 entertainment expense and a $60 meal against policy",
        "reference": "$160 exceeds the $150 entertainment limit; $60 is within the $75 meal limit.",
        "category": "medium_complexity",
        "expected_tool": "expense_mcp_check_expense_policy",
        "expected_signals": ["150", "75"],
        "expected_complexity": "medium",
        "description": "Two policy checks across different categories",
    },
    # ── high: multi-step chains, synthesis, or optimisation ────────────────
    {
        "prompt": (
            "Plan a three-city trip (SFO, Chicago, New York): find flights between each leg, "
            "a hotel in each city within the lodging limit, and total the cost against policy"
        ),
        "reference": "A three-leg itinerary with per-city hotels and a policy-checked cost total.",
        "category": "high_complexity",
        "expected_tool": "search_mcp_search_flights",
        "expected_signals": ["Chicago", "New York"],
        "expected_complexity": "high",
        "description": "Multi-leg planning with synthesis and policy totalling",
    },
    {
        "prompt": (
            "Audit EMP001's expenses: find every over-limit item, total the overage by "
            "category, and recommend which to resubmit with justification"
        ),
        "reference": "Per-category overage totals with resubmission recommendations.",
        "category": "high_complexity",
        "expected_tool": "expense_mcp_get_user_expenses",
        "expected_signals": ["EMP001"],
        "expected_complexity": "high",
        "description": "Retrieval, per-item evaluation, aggregation and recommendation",
    },
    {
        "prompt": (
            "Book the cheapest SFO-JFK flight, reserve a hotel under the lodging limit for "
            "three nights, and submit the expected meal expenses for the trip"
        ),
        "reference": "Flight booked, hotel reserved within policy, and meal expenses submitted.",
        "category": "high_complexity",
        "expected_tool": "booking_mcp_book_flight",
        "expected_signals": ["SFO", "JFK"],
        "expected_complexity": "high",
        "description": "Three chained mutations across all three toolsets",
    },
    {
        "prompt": (
            "Our team of six is travelling to Miami. Work out whether booking individually "
            "or as a group is cheaper, and whether either fits the lodging policy"
        ),
        "reference": "A cost comparison of individual vs group booking, checked against policy.",
        "category": "high_complexity",
        "expected_tool": "search_mcp_search_hotels",
        "expected_signals": ["Miami"],
        "expected_complexity": "high",
        "description": "Optimisation across options with a policy constraint",
    },
    {
        "prompt": (
            "Reconstruct my last trip from my bookings and expenses, then tell me what "
            "I would need to change to keep the same trip fully within policy"
        ),
        "reference": "A reconstructed trip with the specific changes needed for compliance.",
        "category": "high_complexity",
        "expected_tool": "booking_mcp_list_all_bookings",
        "expected_signals": [],
        "expected_complexity": "high",
        "description": "Cross-toolset reconstruction plus counterfactual reasoning",
    },
    {
        "prompt": (
            "Find the cheapest way to get four people from SFO to Chicago and back, "
            "compare it against the transport limit per person, and flag any shortfall"
        ),
        "reference": "A cheapest round-trip option for four, checked per person against $200.",
        "category": "high_complexity",
        "expected_tool": "search_mcp_search_flights",
        "expected_signals": ["SFO", "Chicago"],
        "expected_complexity": "high",
        "description": "Optimisation, per-head arithmetic and a policy comparison",
    },
    {
        "prompt": (
            "Given my expense history, forecast whether a five-night New York trip would "
            "stay within policy, and identify the category most likely to breach"
        ),
        "reference": "A forecast against the lodging and meal limits with the riskiest category.",
        "category": "high_complexity",
        "expected_tool": "expense_mcp_get_user_expenses",
        "expected_signals": ["New York"],
        "expected_complexity": "high",
        "description": "History-grounded forecasting with a risk judgement",
    },
    {
        "prompt": (
            "Cancel my Chicago booking, rebook the same trip at a lower fare if one exists, "
            "and tell me the net saving against the transport limit"
        ),
        "reference": "The booking cancelled, rebooked if cheaper, with the net saving stated.",
        "category": "high_complexity",
        "expected_tool": "booking_mcp_cancel_booking",
        "expected_signals": ["Chicago"],
        "expected_complexity": "high",
        "description": "Conditional multi-step mutation with a computed outcome",
    },
    {
        "prompt": (
            "Build the cheapest policy-compliant two-night Boston trip for two people: "
            "flights, one hotel room, and meals, then tell me the total per person"
        ),
        "reference": "A costed two-night Boston trip for two, each component within its limit.",
        "category": "high_complexity",
        "expected_tool": "search_mcp_search_flights",
        "expected_signals": ["Boston"],
        "expected_complexity": "high",
        "description": "Constrained optimisation across all three toolsets with a per-head total",
    },
]


# ---------------------------------------------------------------------------
# AgentInfo builders
# ---------------------------------------------------------------------------
def build_agent_info(agent_name: str) -> types.evals.AgentInfo:
    """Build AgentInfo manually for offline evaluation without MCP connections."""
    builders = {
        "coordinator_agent": _build_coordinator_info,
        "travel_agent": _build_travel_info,
        "expense_agent": _build_expense_info,
        "router_agent": _build_router_info,
    }
    builder = builders.get(agent_name)
    if builder:
        return builder()
    if agent_name in STANDALONE_AGENTS:
        return _build_standalone_info(agent_name)
    raise ValueError(f"Unknown agent: {agent_name}. Valid: {list(builders) + STANDALONE_AGENTS}")


def _build_coordinator_info() -> types.evals.AgentInfo:
    """Descriptor for the coordinator — single-agent, direct MCP tools.

    It carries no AgentTools (0 measured delegations; a nested sub-agent MCP call
    does not stream on the managed runtime — see
    docs/notes/coordinator-router-learnings.md). travel_agent/expense_agent keep
    their own descriptors below and are still scored standalone.
    """
    return types.evals.AgentInfo(
        name="coordinator_agent",
        root_agent_id="coordinator_agent",
        agents={
            "coordinator_agent": types.evals.AgentConfig(
                agent_id="coordinator_agent",
                agent_type="LlmAgent",
                description="Corporate assistant handling travel and expenses with its own MCP tools.",
                instruction=(
                    "Handle requests directly: search/book flights and hotels, check expense "
                    "policy before submitting, submit expenses, and report past expenses."
                ),
                tools=declared_tools(ALL_MCP_TOOL_NAMES),
                sub_agents=[],
            ),
        },
    )


def _build_travel_info() -> types.evals.AgentInfo:
    return types.evals.AgentInfo(
        name="travel_agent",
        root_agent_id="travel_agent",
        agents={
            "travel_agent": types.evals.AgentConfig(
                agent_id="travel_agent",
                agent_type="LlmAgent",
                description="Corporate travel assistant for searching and booking flights and hotels.",
                instruction=(
                    "Search for flights and hotels using MCP tools. Present options clearly, "
                    "then use booking tools to confirm reservations."
                ),
                tools=declared_tools((*SEARCH_TOOL_NAMES, *BOOKING_TOOL_NAMES)),
                sub_agents=[],
            ),
        },
    )


def _build_expense_info() -> types.evals.AgentInfo:
    return types.evals.AgentInfo(
        name="expense_agent",
        root_agent_id="expense_agent",
        agents={
            "expense_agent": types.evals.AgentConfig(
                agent_id="expense_agent",
                agent_type="LlmAgent",
                description="Corporate expense management assistant.",
                instruction=(
                    "Policy limits: meals ($75), transport ($200), lodging ($400), "
                    "supplies ($100), entertainment ($150). Check policy first, "
                    "submit expenses, view history."
                ),
                tools=declared_tools(EXPENSE_TOOL_NAMES),
                sub_agents=[],
            ),
        },
    )


def _build_router_info() -> types.evals.AgentInfo:
    """Descriptor for the router — ONE direct-tools agent, five backbones.

    Deliberately a single agent with no sub_agents. The router stopped delegating
    on 2026-08-20 (docs/notes/router-transfer-streaming.md): ``transfer_to_agent``
    never streamed the specialist's turn through the managed runtime, so the five
    tier agents were collapsed into one agent that holds the MCP toolsets directly
    and swaps its MODEL and INSTRUCTION per tier via ``TierRoutingLlm`` and
    ``tier_instruction_provider`` (src/router/agents.py:298-313).

    Keeping the old five-``sub_agents`` topology here described an architecture
    that can no longer run, and the ``transfer_to_agent`` it implied was also the
    thing that used to put a ``function_call`` in every trace by construction —
    which is why removing it silently broke ``tool_use_quality_v1``. See
    docs/notes/router-tool-use-quality.md.
    """
    return types.evals.AgentInfo(
        name="router_agent",
        root_agent_id="router_agent",
        agents={
            "router_agent": types.evals.AgentConfig(
                agent_id="router_agent",
                agent_type="LlmAgent",
                description=(
                    "Corporate travel and expense assistant with its own MCP tools. A "
                    "complexity classifier picks one of five backbones per request; the "
                    "agent itself is the same one every time and answers directly."
                ),
                instruction=(
                    "Fulfill the request DIRECTLY using your tools: search/book flights "
                    "and hotels, check expense policy before submitting, submit expenses, "
                    "and report past expenses. Ask for missing details when intent is "
                    "unclear rather than guessing."
                ),
                tools=declared_tools(ALL_MCP_TOOL_NAMES),
                sub_agents=[],
            ),
        },
    )


# ---------------------------------------------------------------------------
# Standalone agent AgentInfo builders
# ---------------------------------------------------------------------------
STANDALONE_AGENTS = ["lite_agent", "flash_agent", "pro_agent", "sonnet_agent", "opus_agent"]

_STANDALONE_DESCRIPTIONS = {
    "lite_agent": "Handles trivial, single-intent lookups.",
    "flash_agent": "Handles simple tasks with light reasoning.",
    "pro_agent": "Handles moderate tasks requiring reasoning.",
    "sonnet_agent": "Handles complex, multi-intent requests.",
    "opus_agent": "Handles expert-level requests requiring deep planning.",
}


def _build_standalone_info(agent_name: str) -> types.evals.AgentInfo:
    return types.evals.AgentInfo(
        name=agent_name,
        root_agent_id=agent_name,
        agents={
            agent_name: types.evals.AgentConfig(
                agent_id=agent_name,
                agent_type="LlmAgent",
                description=_STANDALONE_DESCRIPTIONS[agent_name],
                instruction="Corporate assistant with access to travel and expense tools.",
                tools=declared_tools(ALL_MCP_TOOL_NAMES),
                sub_agents=[],
            ),
        },
    )


STANDALONE_EVAL_CASES = TRAVEL_EVAL_CASES + EXPENSE_EVAL_CASES


# ---------------------------------------------------------------------------
# Test case and metric selectors
# ---------------------------------------------------------------------------
ALL_AGENTS = ["coordinator_agent", "travel_agent", "expense_agent", "router_agent"]

_EVAL_CASES = {
    "coordinator_agent": COORDINATOR_EVAL_CASES,
    "travel_agent": TRAVEL_EVAL_CASES,
    "expense_agent": EXPENSE_EVAL_CASES,
    "router_agent": ROUTER_EVAL_CASES,
    "lite_agent": STANDALONE_EVAL_CASES,
    "flash_agent": STANDALONE_EVAL_CASES,
    "pro_agent": STANDALONE_EVAL_CASES,
    "sonnet_agent": STANDALONE_EVAL_CASES,
    "opus_agent": STANDALONE_EVAL_CASES,
}


def get_eval_cases(agent_name: str) -> list[dict]:
    """Return the test case list for the given agent."""
    cases = _EVAL_CASES.get(agent_name)
    if cases is None:
        raise ValueError(f"Unknown agent: {agent_name}. Valid: {list(_EVAL_CASES)}")
    return cases


def get_metrics(agent_name: str) -> list:
    """Return the appropriate evaluation metrics for the given agent.

    Note: ``policy_compliance`` is deliberately NOT included here. The custom
    pointwise ``POLICY_COMPLIANCE_METRIC`` cannot be scored through
    ``client.evals`` in the installed vertexai SDK (the judge scores correctly
    but the service's parser rejects its markdown verdict as invalid JSON, so
    every case errors and the metric is dropped). It is instead scored by the
    standalone judge in :mod:`src.eval.policy_judge`, which the offline-eval
    bridge (``publish_offline_eval.py``) uses to feed the monitored metric.
    """
    return [
        types.RubricMetric.FINAL_RESPONSE_QUALITY,
        types.RubricMetric.HALLUCINATION,
        types.RubricMetric.SAFETY,
        types.RubricMetric.TOOL_USE_QUALITY,
        types.RubricMetric.INSTRUCTION_FOLLOWING,
        types.RubricMetric.FINAL_RESPONSE_MATCH,
    ]


def get_multi_turn_metrics() -> list:
    """Return the multi-turn adaptive rubric metrics for simulated eval.

    These score whole conversations, not single turns, so they belong only on
    the multi-turn simulated-eval path (``src/eval/simulated_eval.py``) — never
    the single-turn 6-rubric batch (``get_metrics``). Together they cover the
    three axes the platform grades a multi-turn agent on: did it accomplish the
    user's goal (task success), did it call the right tools well along the way
    (tool-use quality), and was the overall path coherent (trajectory quality).
    """
    return [
        types.RubricMetric.MULTI_TURN_TASK_SUCCESS,
        types.RubricMetric.MULTI_TURN_TOOL_USE_QUALITY,
        types.RubricMetric.MULTI_TURN_TRAJECTORY_QUALITY,
    ]
