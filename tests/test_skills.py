"""Tests for the in-repo skill definitions (src/skills/definitions.py).

These assert the properties that decide whether a published skill *works*, not
that the prose says any particular thing:

* the SKILL.md frontmatter is real YAML and its ``name`` is the id the registry
  will be given (the two are a package/metadata pair — a mismatch publishes a
  skill under one id that announces itself as another);
* ``description`` is non-empty and written as a usage cue, because semantic
  retrieval matches on it — an empty/undescriptive one makes the skill
  undiscoverable while still looking published;
* ``skill_id`` survives the repo's own id slugifier unchanged;
* every tool the instruction body tells the agent to call actually exists on one
  of the MCP servers, **and** every backticked response field it tells the agent
  to read actually exists on one of those tools' responses (a skill that
  references an invented tool or field is worse than no skill: it teaches a
  procedure the agent cannot execute — it calls the tool, cannot find the field,
  and either stalls or invents a value);
* the materializer really writes a readable SKILL.md, into a *distinct*
  directory per call.
"""

import re
from pathlib import Path

import pytest
import yaml

from src.registry import _slug
from src.skills import (
    SKILL_DEFINITIONS,
    SkillDefinition,
    get_skill,
    materialize_skill,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MCP_SERVERS_DIR = REPO_ROOT / "src" / "mcp_servers"


def _split_frontmatter(skill_md: str) -> tuple[str, str]:
    """Return (frontmatter_yaml, body) for a SKILL.md string."""
    assert skill_md.startswith("---\n"), "SKILL.md must open with a YAML frontmatter fence"
    end = skill_md.find("\n---\n", 3)
    assert end != -1, "SKILL.md frontmatter is not closed by a '---' fence"
    return skill_md[4:end], skill_md[end + len("\n---\n") :]


def _real_mcp_tool_names() -> set[str]:
    """Tool names actually exposed by the three MCP servers, read from source."""
    names: set[str] = set()
    for server in sorted(MCP_SERVERS_DIR.glob("*/server.py")):
        names.update(re.findall(r"@mcp\.tool\(\)\s*\ndef\s+(\w+)", server.read_text()))
    return names


def _keys_of(value: object) -> set[str]:
    """Every dict key reachable inside `value`, including nested ones."""
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            keys.add(str(key))
            keys |= _keys_of(nested)
    elif isinstance(value, list):
        for item in value:
            keys |= _keys_of(item)
    return keys


def _real_tool_response_fields() -> dict[str, set[str]]:
    """Map each MCP tool name to the fields its response can actually carry.

    Derived by **running the real mock DBs**, not by grepping for string
    literals: a booking record is assembled across two files (`create_booking`
    splats a `details` dict that the server builds), so only executing it gives
    the true shape.

    Per-tool, not a flat vocabulary, and that distinction is the whole point.
    `price` is a real field *somewhere* on this surface — it is just not on a
    booking record — so a union-of-everything check calls
    `get_booking_details(...).price` grounded and misses precisely the bug this
    test exists to catch.
    """
    from src.mcp_servers.booking import mock_db as booking_db
    from src.mcp_servers.expense import mock_db as expense_db
    from src.mcp_servers.search import mock_db as search_db

    # These module-level dicts are process-wide accumulators shared with other
    # test modules; probe them on a clean slate and put the contents back.
    saved_bookings, saved_expenses = dict(booking_db.bookings), dict(expense_db.expenses)
    try:
        booking_db.bookings.clear()
        expense_db.expenses.clear()

        flight = booking_db.create_booking("flight", "FL001", {"passenger_name": "Ada Lovelace"})
        hotel = booking_db.create_booking(
            "hotel",
            "HT001",
            {"guest_name": "Ada Lovelace", "checkin": "2026-06-15", "checkout": "2026-06-18"},
        )
        # Snapshot before cancelling: `cancel_booking` mutates the stored record
        # in place, so `cancelled_at` would otherwise leak into book_flight's shape.
        flight_fields, hotel_fields = _keys_of(flight), _keys_of(hotel)
        cancelled_fields = _keys_of(booking_db.cancel_booking(flight["booking_id"]))
        listing_fields = _keys_of(booking_db.list_bookings())

        expense_fields = _keys_of(expense_db.submit_expense(120.0, "lodging", "probe", "EMP001"))
        expense_listing_fields = _keys_of(expense_db.get_expenses("EMP001"))
        # Both branches: a scored check and the unknown-category one.
        policy_fields = _keys_of(expense_db.check_policy(50.0, "meals")) | _keys_of(
            expense_db.check_policy(50.0, "not-a-category")
        )
    finally:
        booking_db.bookings.clear()
        booking_db.bookings.update(saved_bookings)
        expense_db.expenses.clear()
        expense_db.expenses.update(saved_expenses)

    # Every booking lookup can also return the not-found branch, `{"error": ...}`.
    any_booking = flight_fields | hotel_fields | cancelled_fields | {"error"}
    return {
        "search_flights": _keys_of(search_db.FLIGHTS),
        "search_hotels": _keys_of(search_db.HOTELS),
        "book_flight": flight_fields,
        "book_hotel": hotel_fields,
        "cancel_booking": any_booking,
        "get_booking_details": any_booking,
        "list_all_bookings": listing_fields,
        "check_expense_policy": policy_fields,
        "submit_expense": expense_fields,
        "get_user_expenses": expense_listing_fields,
    }


def _policy_category_vocabulary() -> set[str]:
    """The accepted `category` *values* (`meals`, `lodging`, ...).

    Backticked all over the triage skill, and legitimately so, but they are
    values of the `category` field rather than fields — derived from
    `POLICY_LIMITS` so the set cannot drift from the tool's real vocabulary.
    """
    from src.mcp_servers.expense.mock_db import POLICY_LIMITS

    return set(POLICY_LIMITS)


# Backticked lowercase words in a skill body that are deliberately NOT fields.
# Keep this list short — every entry weakens the grounding check.
#   * `cancelled` — a *value* of the booking record's `status` field
#     (`cancel_booking` sets `status = "cancelled"`). The shapes above are built
#     from response keys, so a value can never appear in them.
_NON_FIELD_BACKTICKED_TERMS = {"cancelled"}


def _referenced_field_names(skill_md: str) -> set[str]:
    """Bare backticked identifiers in a skill body, i.e. its response-field claims.

    Matches `` `truncated` `` and `` `within_policy: false` `` (a field quoted
    with an example value) but deliberately not `` `search_hotels(city)` `` —
    call sites are a tool reference, graded against the tool names instead.
    """
    referenced: set[str] = set()
    for span in re.findall(r"`([^`\n]+)`", skill_md):
        match = re.fullmatch(r"([a-z][a-z0-9_]*)(?::\s.*)?", span)
        if match:
            referenced.add(match.group(1))
    return referenced - _NON_FIELD_BACKTICKED_TERMS


@pytest.fixture(params=SKILL_DEFINITIONS, ids=lambda s: s.skill_id)
def skill(request) -> SkillDefinition:
    return request.param


def test_three_skills_are_defined():
    assert len(SKILL_DEFINITIONS) == 3
    assert len({s.skill_id for s in SKILL_DEFINITIONS}) == 3, "skill ids must be unique"


def test_mcp_tool_extraction_finds_the_real_surface():
    """Guard the guard: if this regex ever finds nothing, the grounding test below
    would pass vacuously."""
    tools = _real_mcp_tool_names()
    assert {"search_flights", "book_flight", "check_expense_policy"} <= tools
    assert len(tools) >= 9


def test_mcp_response_field_extraction_finds_the_real_surface():
    """Guard the guard, field edition: a field check that matches nothing (or that
    accepts everything) would pass vacuously and be worse than no check at all."""
    shapes = _real_tool_response_fields()
    assert set(shapes) == _real_mcp_tool_names(), "the shape map has drifted from the tool surface"
    assert all(shapes.values()), f"empty shape: {sorted(k for k, v in shapes.items() if not v)}"

    assert {"booking_id", "item_id", "created_at", "status"} <= shapes["get_booking_details"]
    assert {"guest_name", "checkin", "checkout"} <= shapes["book_hotel"]
    assert {"price", "date", "departure", "arrival"} <= shapes["search_flights"]
    assert {"price_per_night", "city"} <= shapes["search_hotels"]
    assert {"within_policy", "limit", "reason"} <= shapes["check_expense_policy"]
    assert {"expense_id", "total_count", "truncated"} <= shapes["get_user_expenses"]

    # The negative half, and the actual Issue-1 property: a booking record carries
    # neither a price nor a travel date. If these ever start passing, the check
    # below has silently stopped being able to fail.
    assert not {"price", "price_per_night", "date"} & shapes["get_booking_details"]

    # ...and the skill bodies must really make field claims for any of it to grade.
    referenced = set().union(*(_referenced_field_names(s.skill_md) for s in SKILL_DEFINITIONS))
    assert {"item_id", "price", "checkin", "truncated"} <= referenced
    assert len(referenced) >= 15


class TestFrontmatter:
    def test_parses_as_yaml_with_name_and_description(self, skill: SkillDefinition):
        frontmatter, _ = _split_frontmatter(skill.skill_md)
        parsed = yaml.safe_load(frontmatter)
        assert isinstance(parsed, dict)
        assert set(parsed) == {"name", "description"}

    def test_name_matches_skill_id(self, skill: SkillDefinition):
        # The registry treats frontmatter `name` as the skill package identifier;
        # publishing under a different `skill_id` splits the skill's identity.
        frontmatter, _ = _split_frontmatter(skill.skill_md)
        assert yaml.safe_load(frontmatter)["name"] == skill.skill_id

    def test_description_matches_the_registered_description(self, skill: SkillDefinition):
        frontmatter, _ = _split_frontmatter(skill.skill_md)
        assert yaml.safe_load(frontmatter)["description"] == skill.description

    def test_body_is_a_real_markdown_instruction(self, skill: SkillDefinition):
        _, body = _split_frontmatter(skill.skill_md)
        assert body.lstrip().startswith("# "), "body should open with a markdown H1"
        assert len(body.split()) > 80, "an instruction body this short teaches nothing"


class TestDescription:
    def test_is_non_empty(self, skill: SkillDefinition):
        # Semantic retrieval matches on this field: empty means undiscoverable.
        assert skill.description.strip()
        assert len(skill.description.split()) >= 12

    def test_is_written_as_a_retrieval_cue(self, skill: SkillDefinition):
        # Convention from the Skill Registry docs: the description says *when* to
        # use the skill, not only what it is, because that is the text semantic
        # search scores a user's request against.
        assert "use this skill when" in skill.description.lower()

    def test_display_name_is_present_and_human_readable(self, skill: SkillDefinition):
        assert skill.display_name.strip()
        assert skill.display_name != skill.skill_id


class TestSkillId:
    def test_is_registry_safe(self, skill: SkillDefinition):
        # `_slug` is the same derivation src/registry.py uses for agent ids;
        # a slug-stable id is one the registry will accept verbatim.
        assert _slug(skill.skill_id) == skill.skill_id

    def test_get_skill_round_trips(self, skill: SkillDefinition):
        assert get_skill(skill.skill_id) is skill

    def test_get_skill_rejects_unknown_id(self):
        with pytest.raises(KeyError):
            get_skill("no-such-skill")


class TestGroundedInRealTools:
    def test_every_referenced_tool_exists(self, skill: SkillDefinition):
        real = _real_mcp_tool_names()
        # Backticked call sites, e.g. `check_expense_policy(amount, category)`.
        referenced = set(re.findall(r"`([a-z][a-z0-9_]+)\(", skill.skill_md))
        assert referenced, "a procedural skill should name the tools it drives"
        assert referenced <= real, f"invented tools: {sorted(referenced - real)}"

    def test_every_referenced_field_is_reachable_from_a_tool_the_skill_calls(
        self, skill: SkillDefinition
    ):
        """A skill may only name fields carried by tools it actually tells the agent
        to call.

        The tool check above grades call sites only, so a body could read a field
        off a response that has no such field and still pass — which is exactly
        what happened: receipt-audit told the agent to take `price` and `date`
        from a `get_booking_details` record that carries neither, while never
        instructing it to search. Scoping the allowed fields to the called tools
        is what makes that a failure: it forces step 1 to fetch whatever the
        later steps consume.
        """
        shapes = _real_tool_response_fields()
        called = set(re.findall(r"`([a-z][a-z0-9_]+)\(", skill.skill_md)) & set(shapes)
        assert called, "a procedural skill should name the tools it drives"

        reachable = set().union(*(shapes[tool] for tool in called)) | _policy_category_vocabulary()
        referenced = _referenced_field_names(skill.skill_md)
        assert referenced, "a grounded skill should name the response fields it reads"
        unreachable = referenced - reachable
        assert not unreachable, (
            f"{skill.skill_id} reads {sorted(unreachable)}, which no response of its called "
            f"tools ({sorted(called)}) carries"
        )

    def test_skills_collectively_cover_all_three_mcp_servers(self):
        referenced: set[str] = set()
        for definition in SKILL_DEFINITIONS:
            referenced.update(re.findall(r"`([a-z][a-z0-9_]+)\(", definition.skill_md))
        assert {"search_flights", "search_hotels"} & referenced  # search
        assert {"book_flight", "book_hotel", "get_booking_details"} & referenced  # booking
        assert {"check_expense_policy", "submit_expense"} & referenced  # expense


class TestMaterializeSkill:
    def test_writes_a_readable_skill_md(self, skill: SkillDefinition):
        with materialize_skill(skill) as directory:
            skill_md = directory / "SKILL.md"
            assert skill_md.is_file()
            assert skill_md.read_text(encoding="utf-8") == skill.skill_md

    def test_directory_is_removed_on_exit(self, skill: SkillDefinition):
        with materialize_skill(skill) as directory:
            pass
        assert not directory.exists()

    def test_each_call_gets_its_own_directory(self):
        # The whole reason this is a TemporaryDirectory and not the notebook's
        # fixed /tmp/sample_math_skill: concurrent/repeat publishes must not
        # write over each other.
        definition = SKILL_DEFINITIONS[0]
        with materialize_skill(definition) as first, materialize_skill(definition) as second:
            assert first != second
            assert (first / "SKILL.md").read_text() == (second / "SKILL.md").read_text()

    def test_directory_contains_only_skill_md(self, skill: SkillDefinition):
        # Instruction-only skills: no scripts, so nothing else should be packaged.
        with materialize_skill(skill) as directory:
            assert [p.name for p in directory.iterdir()] == ["SKILL.md"]
