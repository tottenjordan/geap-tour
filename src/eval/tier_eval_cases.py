"""Complexity-tiered eval cases for the cross-model experiment.

Each tier contains 7 cases matched to a specific complexity level:
- Low: single tool call, direct lookup
- Medium: 2 tools, comparison, or multi-step reasoning
- High: 3+ tools, cross-domain analysis, budget optimization
"""

LOW_COMPLEXITY_CASES = [
    {
        "prompt": "Find flights from SFO to JFK",
        "reference": "Flights from SFO to JFK: United FL001 at $450, Delta FL002 at $520.",
        "category": "low",
        "expected_tool": "search_mcp_search_flights",
        "expected_signals": ["SFO", "JFK"],
        "description": "Simple flight search",
    },
    {
        "prompt": "Search for hotels in New York",
        "reference": "Grand Hyatt at $320/night (4.5 stars) and Budget Inn at $120/night (3.2 stars).",
        "category": "low",
        "expected_tool": "search_mcp_search_hotels",
        "expected_signals": ["Grand Hyatt", "Budget Inn"],
        "description": "Simple hotel search",
    },
    {
        "prompt": "What is the meal expense limit?",
        "reference": "The corporate meal expense limit is $75. Amounts above require manager review.",
        "category": "low",
        "expected_tool": "expense_mcp_check_expense_policy",
        "expected_signals": ["75", "meal"],
        "description": "Simple policy lookup",
    },
    {
        "prompt": "Book flight FL001 for Alice Johnson",
        "reference": "Flight FL001 booked and confirmed for Alice Johnson.",
        "category": "low",
        "expected_tool": "booking_mcp_book_flight",
        "expected_signals": ["FL001", "Alice Johnson", "confirmed"],
        "description": "Simple booking",
    },
    {
        "prompt": "Show expenses for user EMP001",
        "reference": "Expense history for EMP001 retrieved.",
        "category": "low",
        "expected_tool": "expense_mcp_get_user_expenses",
        "expected_signals": ["EMP001"],
        "description": "Simple history retrieval",
    },
    {
        "prompt": "Is a $50 transport expense within policy?",
        "reference": "A $50 transport expense is within the $200 policy limit.",
        "category": "low",
        "expected_tool": "expense_mcp_check_expense_policy",
        "expected_signals": ["within", "200"],
        "description": "Simple policy check",
    },
    {
        "prompt": "Find me a hotel in Miami",
        "reference": "Fontainebleau Miami at $400/night with a 4.7 rating.",
        "category": "low",
        "expected_tool": "search_mcp_search_hotels",
        "expected_signals": ["Fontainebleau", "Miami"],
        "description": "Simple hotel search",
    },
]

MEDIUM_COMPLEXITY_CASES = [
    {
        "prompt": "Submit a $45 meals expense for lunch meeting, user ID EMP001",
        "reference": "Policy checked ($45 within $75 limit). Expense submitted for EMP001, status: approved.",
        "category": "medium",
        "expected_tool": "expense_mcp_submit_expense",
        "expected_signals": ["EMP001", "45", "approved"],
        "description": "Submit with policy check",
    },
    {
        "prompt": "Find flights to NYC and compare the cheapest options by airline",
        "reference": "United FL001 at $450 vs Delta FL002 at $520. United is $70 cheaper (13.5% savings).",
        "category": "medium",
        "expected_tool": "search_mcp_search_flights",
        "expected_signals": ["NYC", "FL001", "FL002"],
        "description": "Flight comparison",
    },
    {
        "prompt": "Search hotels in New York, then check if the nightly rate fits our lodging policy",
        "reference": "Grand Hyatt $320/night within $400 policy. Budget Inn $120/night within policy.",
        "category": "medium",
        "expected_tool": "search_mcp_search_hotels",
        "expected_signals": ["400", "within"],
        "description": "Search + policy cross-check",
    },
    {
        "prompt": "Check if a $100 meal and a $250 entertainment expense are both within policy",
        "reference": "Meals $100 exceeds $75 limit. Entertainment $250 exceeds $150 limit. Both need manager review.",
        "category": "medium",
        "expected_tool": "expense_mcp_check_expense_policy",
        "expected_signals": ["exceeds", "75", "150"],
        "description": "Multi-category policy check",
    },
    {
        "prompt": "Show expense history for EMP001 and flag any items that exceeded policy limits",
        "reference": "EMP001 expenses reviewed against policy limits. Violations flagged with overage amounts.",
        "category": "medium",
        "expected_tool": "expense_mcp_get_user_expenses",
        "expected_signals": ["EMP001"],
        "description": "History with policy analysis",
    },
    {
        "prompt": "Find the cheapest flight from SFO to JFK and tell me how much I'd save vs the most expensive",
        "reference": "Cheapest: United FL001 at $450. Most expensive: Delta FL002 at $520. Savings: $70 (13.5%).",
        "category": "medium",
        "expected_tool": "search_mcp_search_flights",
        "expected_signals": ["SFO", "JFK", "450", "520"],
        "description": "Price comparison with savings",
    },
    {
        "prompt": "Book hotel HT002 for Bob Smith June 15-18, and check if it's within lodging policy",
        "reference": "Hotel HT002 booked for Bob Smith. Rate within $400/night lodging policy.",
        "category": "medium",
        "expected_tool": "booking_mcp_book_hotel",
        "expected_signals": ["HT002", "Bob Smith", "400"],
        "description": "Book + policy verify",
    },
]

HIGH_COMPLEXITY_CASES = [
    {
        "prompt": "Plan a 5-day trip to Tokyo for a team of 4: find flights from SFO, hotels, estimate daily meal expenses, and check entertainment policy",
        "reference": "Flights searched. Hotels searched. Meals: $75/person/day x4 = $300/day. Entertainment limit: $150. Total estimated.",
        "category": "high",
        "expected_tool": "multiple",
        "expected_signals": ["Tokyo", "75", "150"],
        "description": "Multi-step team trip planning",
    },
    {
        "prompt": "Compare flights from SFO to JFK vs LAX to ORD, factoring in hotel costs in each destination city",
        "reference": "SFO-JFK: FL001 $450 + NYC hotels. LAX-ORD: FL003 $380 + Chicago hotels. Full cost comparison.",
        "category": "high",
        "expected_tool": "multiple",
        "expected_signals": ["SFO", "JFK", "LAX", "ORD"],
        "description": "Multi-route comparison with hotels",
    },
    {
        "prompt": "Book flight FL001 for Alice, check if Grand Hyatt is within lodging policy, and submit a $75 meals expense for EMP001",
        "reference": "FL001 booked. Grand Hyatt $320 within $400 policy. $75 meals submitted, approved.",
        "category": "high",
        "expected_tool": "multiple",
        "expected_signals": ["FL001", "Alice", "400", "75"],
        "description": "Book + policy + expense pipeline",
    },
    {
        "prompt": "I have a $2000 budget for a London trip. Find flights, hotels, check lodging and meal policies, and tell me if I can afford it",
        "reference": "Flights searched. Hotels searched. Lodging $400/night, meals $75/day. Budget analysis against $2000.",
        "category": "high",
        "expected_tool": "multiple",
        "expected_signals": ["2000", "400", "75"],
        "description": "Budget-constrained trip planning",
    },
    {
        "prompt": "Review EMP002's expense history, check all policy categories, and submit a $150 supplies expense for office equipment for EMP002",
        "reference": "EMP002 history reviewed. All limits checked. $150 supplies exceeds $100 limit, pending review.",
        "category": "high",
        "expected_tool": "multiple",
        "expected_signals": ["EMP002", "100", "exceeds"],
        "description": "Review + audit + submit pipeline",
    },
    {
        "prompt": "Find the cheapest way to fly from SFO to JFK, book it for Bob Smith, find a hotel within policy, and submit a pre-trip expense estimate",
        "reference": "FL001 $450 booked for Bob Smith. Hotel within $400 policy. Pre-trip estimate submitted.",
        "category": "high",
        "expected_tool": "multiple",
        "expected_signals": ["FL001", "Bob Smith", "400"],
        "description": "End-to-end trip booking pipeline",
    },
    {
        "prompt": "Pull expense histories for EMP001 and EMP002, flag policy violations across both, and summarize total overspend by category",
        "reference": "EMP001: no violations. EMP002: entertainment $200 exceeds $150. Total overspend: $50.",
        "category": "high",
        "expected_tool": "multiple",
        "expected_signals": ["EMP001", "EMP002", "150"],
        "description": "Multi-user expense audit",
    },
    # ---------------------------------------------------------------------
    # UPPER high band (target classifier score >= 0.80, i.e. the `pro` tier).
    #
    # Added to settle COMPLEXITY_HIGH. The paired run found sonnet beating pro on
    # BOTH sides of the 0.80 cut, but the upper side had only 6 decisive cases —
    # 6-0 reaches p=0.0312, the thinnest significance obtainable, and a single loss
    # would have made it 5-1 (p=0.219, nothing). Twelve more prompts in that band
    # take the decisive n high enough for the result to mean something either way.
    #
    # Written to the shape that actually scores >= 0.85 on this classifier, read
    # off the per-case scores of the previous run rather than guessed: three tool
    # DOMAINS in one request (search + booking + expense), an explicit optimisation
    # or comparison across alternatives, or a stateful mutation (cancel / rebook /
    # reconstruct). The 0.75 cases are multi-step but narrower — usually one domain
    # plus a policy lookup. Every reference uses only real POLICY_LIMITS values
    # (meals 75 / transport 200 / lodging 400 / supplies 100 / entertainment 150);
    # inventing policy is what `tests/test_rubric_grounding.py` exists to stop.
    {
        "prompt": "Plan SFO -> Seattle -> Portland -> SFO for two people: cheapest flight on each leg, one hotel per city under the lodging limit, then total it and tell me which leg breaks policy",
        "reference": "Three legs searched and priced, one hotel per city checked against the $400 lodging limit, per-leg and grand total given, and any over-limit leg named.",
        "category": "high",
        "expected_tool": "multiple",
        "expected_signals": ["Seattle", "Portland", "400"],
        "description": "Three-leg multi-city routing with a per-leg policy check and a total",
    },
    {
        "prompt": "Cancel my Chicago booking, search for a cheaper fare on the same dates, rebook it if one exists, submit the fare difference as a transport expense for EMP001, and tell me whether the new total still fits the transport limit",
        "reference": "Existing Chicago booking cancelled, cheapest equivalent rebooked, the fare delta computed and submitted as transport (limit $200) for EMP001.",
        "category": "high",
        "expected_tool": "multiple",
        "expected_signals": ["cancel", "EMP001", "200"],
        "description": "Stateful cancel -> rebook -> expense-the-difference chain",
    },
    {
        "prompt": "Pull EMP001's bookings and full expense history, reconstruct the average cost per trip across flights, lodging and meals, then tell me how many more trips fit inside a remaining $5000 budget and which category would run out first",
        "reference": "Bookings and expenses both retrieved, average per-trip cost derived from them, and the remaining $5000 divided by that average to give a trip count.",
        "category": "high",
        "expected_tool": "multiple",
        "expected_signals": ["EMP001", "5000", "average"],
        "description": "Cross-source reconstruction (bookings + expenses) feeding a budget forecast",
    },
    {
        "prompt": "Book the cheapest SFO-JFK flight; if the nearest hotel to 350 5th Ave is over the lodging limit, book the next cheapest instead, then submit both as expenses for EMP002",
        "reference": "Cheapest SFO-JFK flight booked, nearest hotel checked against the $400 lodging limit with the fallback applied if it exceeds, and both submitted as expenses for EMP002.",
        "category": "high",
        "expected_tool": "multiple",
        "expected_signals": ["JFK", "400", "EMP002"],
        "description": "Conditional booking branch driven by a policy limit, then expensed",
    },
    {
        "prompt": "Compare a Denver team retreat for six booked individually against the same trip booked as a group: price flights each way, a hotel for three nights, and per-diem meals, check each component against policy, then recommend the cheaper option and submit its lodging cost as an expense",
        "reference": "Both booking strategies priced across flights, hotel and meals at $75/person/day, compared, and the cheaper one recommended with the difference stated.",
        "category": "high",
        "expected_tool": "multiple",
        "expected_signals": ["Denver", "75", "group"],
        "description": "Group-vs-individual optimisation across three cost components",
    },
    {
        "prompt": "Audit EMP001 and EMP002 for over-limit expenses across every category, total the overage per person, submit a corrected $90 supplies claim for whoever is under budget, book nothing, and report the remaining headroom per category for both",
        "reference": "Both histories audited against the real category limits, a $90 supplies claim (limit $100) submitted for the employee under budget, and remaining headroom reported per person.",
        "category": "high",
        "expected_tool": "multiple",
        "expected_signals": ["EMP001", "EMP002", "100"],
        "description": "Two-employee audit with a conditional remediation submission",
    },
    {
        "prompt": "Price two candidate New York itineraries end to end — flights plus Grand Hyatt for three nights, versus flights plus Budget Inn for five — add per-diem meals to each, check both against the lodging limit, book the cheaper one and submit its lodging expense for EMP001",
        "reference": "Both itineraries priced in full, compared on total cost, and each checked against the $400 lodging limit.",
        "category": "high",
        "expected_tool": "multiple",
        "expected_signals": ["Grand Hyatt", "Budget Inn", "400"],
        "description": "Head-to-head itinerary comparison on cost and policy simultaneously",
    },
    {
        "prompt": "I have $2500 for a Boston trip for two. Find flights and a hotel, keep meals inside the per-diem, submit the lodging expense for EMP001, and tell me what is left over",
        "reference": "Flights and hotel found inside the $2500 budget, meals kept at or under $75/person/day, lodging submitted for EMP001, and the remaining balance reported.",
        "category": "high",
        "expected_tool": "multiple",
        "expected_signals": ["Boston", "2500", "75"],
        "description": "Hard budget constraint spanning search, booking, per-diem and submission",
    },
    {
        "prompt": "List every booking I currently hold, cross-reference them against my expense history to find which are already claimed, total the uncommitted spend, and flag any that would exceed the lodging or transport limits if expensed today",
        "reference": "All current bookings listed, total committed spend summed, and each checked against the $400 lodging and $200 transport limits.",
        "category": "high",
        "expected_tool": "multiple",
        "expected_signals": ["400", "200", "total"],
        "description": "Full booking sweep cross-checked against two different category limits",
    },
    {
        "prompt": "Find the cheapest four-person SFO to Chicago round trip, compare it against flying two people twice, and submit the winning option's cost as a transport expense for EMP002",
        "reference": "Both strategies priced, the cheaper identified, and its cost submitted as transport (limit $200) for EMP002 with any overage flagged.",
        "category": "high",
        "expected_tool": "multiple",
        "expected_signals": ["Chicago", "EMP002", "200"],
        "description": "Optimisation across two booking strategies, then expensed with a limit check",
    },
    {
        "prompt": "Reconstruct what my last New York trip cost from bookings and expenses, then build a cheaper version of the same trip and show me the saving line by line",
        "reference": "Prior trip reconstructed from both sources, a cheaper equivalent assembled from current inventory, and a line-by-line saving shown.",
        "category": "high",
        "expected_tool": "multiple",
        "expected_signals": ["New York", "saving", "total"],
        "description": "Reconstruction from two sources feeding a re-plan with a line-item diff",
    },
    {
        "prompt": "For a five-night London trip for three, check whether lodging, meals and entertainment all stay inside policy, book the compliant option, and submit every expense category",
        "reference": "Lodging ($400), meals ($75/person/day) and entertainment ($150) all checked, the compliant option booked, and each category submitted.",
        "category": "high",
        "expected_tool": "multiple",
        "expected_signals": ["London", "150", "75"],
        "description": "Three simultaneous category checks, a booking, and multi-category submission",
    },
]

TIER_EVAL_CASES = {
    "low": LOW_COMPLEXITY_CASES,
    "medium": MEDIUM_COMPLEXITY_CASES,
    "high": HIGH_COMPLEXITY_CASES,
}
