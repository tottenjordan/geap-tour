"""Travel/expense skill definitions for the Gemini Enterprise Skill Registry.

**The registry is a deployment target, not the source of truth.** A skill's
whole payload is a `SKILL.md` — YAML frontmatter (`name`, `description`) plus a
Markdown instruction body — and prose that steers a production agent deserves
the same review as the agent's own prompt. So the content lives here, in git,
where it is diffable and reviewable, and the publisher (a separate module) only
uploads it. Same reasoning as ``src/optimize/*sampler_config.json``.

**These are instruction-only skills — no executable scripts, deliberately.**
ADK's ``SkillToolset`` accepts a ``code_executor`` for skills that ship scripts;
we do not use it. Doing so would drag a sandbox and an arbitrary-code-execution
surface into a demo that gains nothing from either. Nobody should "finish" this
by adding one.

**The design constraint that makes these worth publishing:** each skill must
encode procedure the coordinator's ``INSTRUCTION``
(``src/agents/coordinator_agent.py``) does *not* already contain. That
instruction already covers searching, booking, booking management, calling
``check_expense_policy`` before every submission, the flat category limits, the
submit-and-flag rule for over-policy expenses, and proactive next steps. A skill
that restates any of that proves nothing about skill discovery, because the
agent would behave identically without it. The three below therefore cover
*judgement between* those tool calls: how to decompose an ambiguous receipt into
checkable line items, how to assemble and present a multi-leg itinerary before
booking any of it, and how to reconcile a receipt against the booking record it
claims to cover.

The bodies are grounded in the real MCP tool surface (``src/mcp_servers/``) —
real tool names, real argument names, real response fields — because a skill
that references an invented tool teaches a procedure the agent cannot execute.
"""

import contextlib
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

SKILL_MD_FILENAME = "SKILL.md"


@dataclass(frozen=True)
class SkillDefinition:
    """One publishable skill: registry metadata plus its `SKILL.md` payload.

    ``skill_id`` is both the registry id and the frontmatter ``name`` (the
    registry treats the frontmatter name as the package identifier, so the two
    must agree). ``description`` is what semantic retrieval scores a user's
    request against at runtime — it is a *usage cue*, not a title.
    """

    skill_id: str
    display_name: str
    description: str
    skill_md: str


def _skill_md(*, skill_id: str, description: str, body: str) -> str:
    """Compose a `SKILL.md` from the same metadata the registry is handed.

    Composing rather than hand-writing the frontmatter is what keeps the
    published ``description`` and the in-file one from drifting apart — a drift
    that is invisible until retrieval matches on one and the console shows the
    other. Descriptions are emitted as plain YAML scalars, so they must not
    contain a ``": "`` sequence; ``tests/test_skills.py`` parses every
    frontmatter as YAML and would fail if one did.
    """
    return f"---\nname: {skill_id}\ndescription: {description}\n---\n{body.strip()}\n"


def _define(*, skill_id: str, display_name: str, description: str, body: str) -> SkillDefinition:
    return SkillDefinition(
        skill_id=skill_id,
        display_name=display_name,
        description=description,
        skill_md=_skill_md(skill_id=skill_id, description=description, body=body),
    )


EXPENSE_POLICY_TRIAGE = _define(
    skill_id="expense-policy-triage",
    display_name="Expense policy triage",
    description=(
        "Decides which corporate expense-policy clauses apply to an ambiguous receipt before it "
        "is checked or submitted. Use this skill when a receipt spans more than one expense "
        "category, is a multi-night lodging total, is in a foreign currency, or does not map "
        "cleanly onto the meals, transport, lodging, supplies and entertainment categories."
    ),
    body="""\
# Expense Policy Triage

`check_expense_policy(amount, category)` scores exactly **one** amount against
**one** category limit, and `submit_expense(amount, category, description, user_id)`
files exactly one line item. Receipts rarely arrive in that shape. This skill
covers the step before the check: turning a receipt into line items the tools
can actually adjudicate.

## Decide the line items first

Split the receipt into one line item per policy category, then run one
`check_expense_policy(...)` and one `submit_expense(...)` per line item. Never
sum across categories and check the total under whichever category is largest: a
$220 client dinner checked as `meals` reads as a large over-limit violation,
while the same receipt split into a $70 own-meal (`meals`) and a $150 hosting
portion (`entertainment`) is two in-policy items. Split first, and each item
carries its own verdict and its own expense_id.

## Category boundaries the receipt does not settle

- **meals vs entertainment** — decided by who was present and why, not by the
  venue. The traveller's own food is `meals`; hosting a customer, candidate or
  team event is `entertainment`. A hotel restaurant charge is still `meals`.
- **transport vs lodging** — airfare, rail, taxi, rideshare and parking are
  `transport`; only the room charge is `lodging`. Room service is `meals`.
- **supplies** — consumables bought for the trip. Durable or IT-issued equipment
  is not a travel expense at all; say so rather than forcing it into a category.

## Lodging is a per-night limit

The lodging limit applies to the **nightly rate**, not the invoice total. Given a
total, divide by the number of nights and check the nightly figure: a 3-night
$900 invoice is $300/night and within policy, whereas checking $900 reports a
violation that does not exist. When the stay came from a booking, confirm the
nights from `get_booking_details(booking_id)` (`checkin` and `checkout`) instead
of trusting the count in the request, and state the nightly rate and night count
you used.

## Foreign currency

`check_expense_policy(...)` and `submit_expense(...)` take USD, and there is no
conversion tool. Never pass a foreign-currency figure as though it were USD. Ask
for the USD amount the card was actually charged; if the user supplies a rate
instead, do the arithmetic, then record the original amount, currency and rate
in the `description` you submit so a reviewer can re-derive the number.

## An unknown category is not a violation

For a category outside the accepted vocabulary the tool returns
`within_policy: false` with an "Unknown category" reason. That is a
*classification* failure, not a policy breach — do not report it to the user as
"your expense exceeds policy". Re-map to the nearest valid category and re-check,
or ask the user which category applies.

## Report the split, not just the verdicts

State how you split the receipt and why, then give each line item's verdict with
the limit it was judged against. Triage decides *which* limit applies; it never
decides whether an item gets filed. Over-limit items are still submitted and
flagged for review.
""",
)


TRIP_PLANNING_BRIEF = _define(
    skill_id="trip-planning-brief",
    display_name="Multi-city trip planning brief",
    description=(
        "Assembles a multi-city itinerary into a single reviewable brief — leg sequencing, "
        "connection buffers, derived hotel nights and a trip total — and gets one confirmation "
        "before booking anything. Use this skill when a travel request involves more than one "
        "flight leg, more than one city, or overnight stays whose dates follow from the flights."
    ),
    body="""\
# Multi-City Trip Planning Brief

`search_flights(origin, destination, date)` searches exactly one leg and
`search_hotels(city, max_price)` exactly one city. A multi-city trip is therefore
a *sequence* of searches you assemble yourself and present as one brief the
traveller confirms once — not a stream of bookings made as each option appears.

## 1. Sequence the legs

Chain them: each leg's destination airport is the next leg's origin. Search one
leg at a time, in travel order, carrying the chosen option's arrival date
forward. Later legs depend on which option was chosen for the earlier ones, so
never search them all against the date in the original request.

## 2. Apply the connection rules

Flight results carry local clock `departure` and `arrival` times and no time
zone, so:

- Allow at least 2 hours between one leg's arrival and the next leg's departure
  for a same-day domestic connection, 3 hours if either leg is international.
- An arrival time earlier than its departure time is an overnight leg: it lands
  the **next** day. Do not chain a morning departure off it, and count that
  night as spent in the air, not in a hotel.
- A connection inside the buffer is not silently dropped. Keep the leg, mark it
  tight in the brief, and let the traveller decide.

## 3. Derive hotel nights from the itinerary

Do not ask for check-in dates you can compute. For each city with an overnight
stay, `checkin` is the arrival date of the inbound leg, `checkout` is the
departure date of the outbound leg, and nights is the difference. Search by city
*name* — `search_hotels("New York")` — because flights use airport codes and
hotels do not; passing an airport code returns nothing and is not the same as
"no availability".

## 4. Pre-check the nightly rate against policy

Before proposing a hotel, run `check_expense_policy(price_per_night, "lodging")`.
A room the traveller can book but cannot expense is a worse recommendation than a
cheaper one. If every option in the city is over the limit, say so and propose
the cheapest, flagged.

## 5. Present the brief, then ask once

Before booking anything, present:

- each leg: id, airline, date, departure and arrival times, price;
- each city: hotel, nights, price per night, and nights x rate;
- the trip total;
- every unresolved gap — a leg with no results, a connection inside the buffer,
  a night with no hotel, a nightly rate over policy.

Then ask for a single confirmation of the whole brief. Do not book a leg because
it looks like the obvious choice.

## 6. Book in itinerary order, and stop on failure

After confirmation, book flights in travel order with
`book_flight(flight_id, passenger_name)`, then hotels with
`book_hotel(hotel_id, guest_name, checkin, checkout)`, reporting each returned
`booking_id` as you go. If one booking fails partway through, **stop** — do not
carry on down the itinerary. Report exactly which segments are booked and their
ids so the traveller can retry or unwind them with `cancel_booking(booking_id)`.
A half-booked trip whose ids were never reported cannot be cleaned up.
""",
)


RECEIPT_AUDIT = _define(
    skill_id="receipt-audit",
    display_name="Receipt audit against bookings",
    description=(
        "Reconciles a receipt against the booking record it claims to cover and names the "
        "discrepancy class before the expense is filed. Use this skill when a user expenses "
        "travel that was booked through this assistant, or when a receipt amount, date or "
        "traveller name may not match the booking."
    ),
    body="""\
# Receipt Audit

An expense that references a trip has a ground truth: the booking record. Check
the receipt against it *before* submitting, and name what differs. A reviewer can
act on "amount_mismatch: receipt $520, booked $450"; nobody can act on "this
looks fine".

## 1. Fetch the ground truth

With a booking id, call `get_booking_details(booking_id)`. Without one, call
`list_all_bookings(limit)` and match on `type`, `item_id`, traveller name and
date. If nothing matches and `truncated` is true, the booking may simply be older
than the returned window — say that, rather than concluding no booking exists.

## 2. Classify the difference by name

Compare the receipt against the record and report every class that applies:

- **no_matching_booking** — no record covers this trip. Not necessarily improper
  (it may have been booked elsewhere); ask before assuming.
- **cancelled_booking_charge** — the record's `status` is `cancelled`. A charge
  against a cancelled booking needs an explanation before it is reimbursable;
  quote the `cancelled_at` timestamp.
- **amount_mismatch** — the receipt total differs from the booked `price`, or
  from `price_per_night` x nights. Quote both figures. Upgrades, fare changes and
  resort fees all land in this class; the explanation is the traveller's.
- **date_mismatch** — receipt dates fall outside the booked `date`, or outside
  the `checkin`/`checkout` range. Extra nights are usually also an
  amount_mismatch; report both.
- **traveller_mismatch** — the receipt name differs from `passenger_name` or
  `guest_name`. Somebody else's ticket is not reimbursable to this user.
- **clean_match** — every compared field agrees. Say so explicitly: a silent
  audit is indistinguishable from no audit.

Quote the two values you compared. Never assert a mismatch without both numbers.

## 3. Check for a duplicate before filing

`submit_expense(...)` mints a new expense_id on every call and nothing
de-duplicates, so re-submitting a receipt creates a second reimbursable record.
Call `get_user_expenses(user_id, limit)` and look for an existing record with the
same amount, category and description. On a match, **ask before submitting** and
quote the existing expense_id. This is the one case where you pause instead of
filing — it is not the over-limit case, where an expense is always submitted and
flagged for review.

## 4. File with the finding attached

Then submit. Put the discrepancy class and both compared values into the
`description` you pass to `submit_expense(...)`. That description is the only
part of the audit a reviewer ever sees, so a finding that lives only in the chat
reply is lost the moment the expense is filed. Tell the user the class by name
and what you compared.
""",
)


SKILL_DEFINITIONS: tuple[SkillDefinition, ...] = (
    EXPENSE_POLICY_TRIAGE,
    TRIP_PLANNING_BRIEF,
    RECEIPT_AUDIT,
)


def get_skill(skill_id: str) -> SkillDefinition:
    """Look one skill up by id. Raises ``KeyError`` for an unknown id."""
    for skill in SKILL_DEFINITIONS:
        if skill.skill_id == skill_id:
            return skill
    known = ", ".join(s.skill_id for s in SKILL_DEFINITIONS)
    raise KeyError(f"Unknown skill_id {skill_id!r}. Known skills: {known}")


@contextlib.contextmanager
def materialize_skill(skill: SkillDefinition) -> Iterator[Path]:
    """Write ``skill`` into a fresh temp directory as `SKILL.md`, and yield the dir.

    The registry client uploads a *directory* (``config={"local_path": ...}``),
    so a definition has to hit the filesystem before it can be published. This
    uses ``tempfile.TemporaryDirectory`` rather than the notebook's fixed
    ``/tmp/sample_math_skill`` on purpose: a fixed path collides between
    concurrent publishes and silently leaves a previous run's `SKILL.md` behind
    for the next one to upload. A context manager (not a bare path return) so
    the directory is guaranteed to be cleaned up even if the publish raises.
    """
    with tempfile.TemporaryDirectory(prefix=f"geap-skill-{skill.skill_id}-") as tmpdir:
        directory = Path(tmpdir)
        (directory / SKILL_MD_FILENAME).write_text(skill.skill_md, encoding="utf-8")
        yield directory
