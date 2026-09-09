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

import functools
import inspect
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import src.skills.publish_skills as publisher
from src.registry import _slug
from src.skills import (
    SKILL_DEFINITIONS,
    SkillDefinition,
    get_skill,
    materialize_skill,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MCP_SERVERS_DIR = REPO_ROOT / "src" / "mcp_servers"

# Every tool the three MCP servers expose. Spelled out rather than counted so a
# tool that silently drops out of `_real_mcp_tool_names`' regex (someone writes
# `async def`, or `@mcp.tool(name=...)`) fails here instead of quietly shrinking
# the surface the grounding checks below grade against.
_EXPECTED_MCP_TOOLS = {
    "search_flights",
    "search_hotels",
    "book_flight",
    "book_hotel",
    "cancel_booking",
    "get_booking_details",
    "list_all_bookings",
    "submit_expense",
    "check_expense_policy",
    "get_user_expenses",
}

# Below this a body is a stub, not a procedure — it cannot encode judgement the
# coordinator's own INSTRUCTION lacks, which is the bar these skills exist to clear.
_MIN_BODY_WORDS = 80

# A description shorter than this cannot say both *what* the skill does and
# *when* to use it, and semantic retrieval scores requests against that text.
_MIN_DESCRIPTION_WORDS = 12

# Collective floor on distinct backticked response fields across all skills. The
# grounding check is only as strong as the number of claims it has to grade: if
# the bodies stopped naming fields it would pass by having nothing to check.
_MIN_DISTINCT_FIELD_CLAIMS = 15


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


@functools.cache
def _real_tool_response_fields() -> dict[str, set[str]]:
    """Map each MCP tool name to the fields its response can actually carry.

    Derived by **running the real mock DBs**, not by grepping for string
    literals: a booking record is assembled across two files (`create_booking`
    splats a `details` dict that the server builds), so only executing it gives
    the true shape. (Two entries are the documented exception — see the
    server-vs-mock-db notes at the seams below.)

    Cached because probing clears and restores two process-wide dicts that other
    test modules share: one call per session is one window in which that state is
    briefly empty, rather than one per test. Sound because the result is a plain
    snapshot of computed key names — it holds no reference to the live dicts, and
    every caller treats it read-only.

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

    # KNOWN DIVERGENCE (server vs mock db) #1: `{"error"}` is a literal, not
    # executed. `cancel_booking`/`get_booking_details` in mock_db return None on a
    # miss; it is server.py that turns that into `{"error": ...}`, and this map is
    # built from the mock-db layer. If that wrapping moves or is renamed, nothing
    # here notices — the literal keeps asserting a shape the server no longer has.
    any_booking = flight_fields | hotel_fields | cancelled_fields | {"error"}
    return {
        # KNOWN DIVERGENCE #2: these two read the catalogue constants directly
        # rather than calling a tool, because the search tools return the bare
        # list and add no envelope of their own — today. They are also the repo's
        # only *unbounded* list-returning MCP tools, so when CLAUDE.md's "bound
        # every list-returning MCP tool" convention reaches them they will gain a
        # `{total_count, returned_count, truncated, flights}` wrapper, this map
        # will still describe the inner record, and the `set(shapes) ==
        # tool_names` drift guard below will not fire (the keys are unchanged).
        # Re-derive both from the tool functions when that lands.
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


def test_skill_ids_are_unique():
    # A duplicate id makes `get_skill` return the first match and the publisher
    # overwrite one skill with the other, both silently.
    assert SKILL_DEFINITIONS, "no skills defined"
    ids = {s.skill_id for s in SKILL_DEFINITIONS}
    assert len(ids) == len(SKILL_DEFINITIONS), f"duplicate skill ids among {sorted(ids)}"


def test_mcp_tool_extraction_finds_the_real_surface():
    """Guard the guard: if this regex ever finds nothing — or quietly finds one
    fewer — the grounding tests below weaken without failing."""
    assert _real_mcp_tool_names() == _EXPECTED_MCP_TOOLS


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
    assert len(referenced) >= _MIN_DISTINCT_FIELD_CLAIMS


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
        assert len(body.split()) > _MIN_BODY_WORDS, "an instruction body this short teaches nothing"


class TestDescription:
    def test_is_non_empty(self, skill: SkillDefinition):
        # Semantic retrieval matches on this field: empty means undiscoverable.
        assert skill.description.strip()
        assert len(skill.description.split()) >= _MIN_DESCRIPTION_WORDS

    def test_is_written_as_a_retrieval_cue(self, skill: SkillDefinition):
        # Convention from the Skill Registry docs: the description says *when* to
        # use the skill, not only what it is, because that is the text semantic
        # search scores a user's request against.
        assert "use this skill when" in skill.description.lower()

    def test_display_name_is_present_and_human_readable(self, skill: SkillDefinition):
        # "Human readable" concretely: a console label, not a second copy of the
        # slug. `!= skill_id` was near-tautological — the id is slug-constrained,
        # so any prose phrase differs from it. These two properties a slug cannot
        # have: whitespace between words, and a capitalised first word.
        assert skill.display_name.strip()
        assert " " in skill.display_name.strip(), f"{skill.display_name!r} reads as a slug"
        assert skill.display_name[:1].isupper(), f"{skill.display_name!r} is not capitalised"


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


# --------------------------------------------------------------------------
# Publisher CLI (src/skills/publish_skills.py)
# --------------------------------------------------------------------------
# The hard part of testing a registry CLI is that a fake client which cheerfully
# answers every method passes whether or not the SDK is being called correctly —
# the failure mode docs/notes/checks-that-cannot-detect-their-own-failure.md is
# about. So `_FakeSkillsApi` below is not a MagicMock: it binds every call
# against the REAL `agentplatform._genai.skills.Skills` signature, validates
# every `config` dict against the REAL config model's field names, checks the
# `local_path` directory really exists (which is only true if the call happens
# inside `materialize_skill`'s `with`), and stores state so idempotency is a
# behavioural property rather than an assertion about call counts.


def _real_skills_signature(method: str) -> inspect.Signature:
    """The installed SDK's signature for `client.skills.<method>`, minus `self`.

    Binding our real call kwargs against this is what makes these tests survive
    the SDK renaming or reordering a parameter: a rename turns into a TypeError
    here instead of a green run against a fake that never cared.
    """
    from agentplatform._genai.skills import Skills

    sig = inspect.signature(getattr(Skills, method))
    return sig.replace(parameters=[p for p in sig.parameters.values() if p.name != "self"])


def _config_field_names(model_name: str) -> set[str]:
    """Field names of a real `agentplatform` skills config model.

    Read off `model_fields` rather than relying on the models' own
    `extra="forbid"`: `src/eval/_sdk_patches._flip_extra_to_ignore()` flips every
    `agentplatform._genai.types` model to `extra="ignore"` process-wide, so if an
    eval test ran first, `model_validate` would silently accept a typo'd key.
    """
    from agentplatform._genai.types import common

    return set(getattr(common, model_name).model_fields)


def _api_error(code: int, status: str, message: str):
    """A `google.genai` APIError shaped the way the transport really builds one."""
    from google.genai import errors

    cls = errors.ClientError if code < 500 else errors.ServerError
    return cls(code, {"error": {"code": code, "status": status, "message": message}})


class _FakeSkillsApi:
    """Stateful stand-in for `client.skills`, validated against the real SDK."""

    def __init__(self) -> None:
        self.stored: dict[str, SimpleNamespace] = {}
        self.calls: list[tuple[str, dict]] = []
        self.payloads: dict[str, str] = {}  # skill_id -> SKILL.md as uploaded
        self.raise_on: dict[str, BaseException] = {}  # method -> error to raise
        self.raise_for_skill: dict[str, BaseException] = {}  # skill_id -> error

    # -- helpers ---------------------------------------------------------
    def _record(self, method: str, kwargs: dict, config_model: str | None = None) -> None:
        _real_skills_signature(method).bind(**kwargs)
        config = kwargs.get("config")
        if config is not None:
            assert config_model is not None, f"{method} was not expected to take a config"
            unknown = set(config) - _config_field_names(config_model)
            assert not unknown, f"{method}(config=...) has keys {sorted(unknown)} the SDK rejects"
        self.calls.append((method, kwargs))
        if method in self.raise_on:
            raise self.raise_on[method]

    def _ingest(self, skill_id: str, local_path: str) -> None:
        directory = Path(local_path)
        assert directory.is_dir(), (
            f"local_path {local_path} does not exist at call time — the create/update call "
            "must happen inside materialize_skill's `with` block"
        )
        assert [p.name for p in directory.iterdir()] == ["SKILL.md"]
        self.payloads[skill_id] = (directory / "SKILL.md").read_text(encoding="utf-8")

    @staticmethod
    def _skill_id_of(name: str) -> str:
        return name.rsplit("/", 1)[-1]

    # -- the SDK surface -------------------------------------------------
    def create(self, **kwargs):
        self._record("create", kwargs, "CreateSkillConfig")
        skill_id = kwargs["skill_id"]
        if skill_id in self.raise_for_skill:
            raise self.raise_for_skill[skill_id]
        name = publisher.skill_resource_name(skill_id)
        if name in self.stored:
            # AIP-standard behaviour for a create against an existing id. This is
            # what makes "publish twice" a red test if the lookup is removed.
            raise _api_error(409, "ALREADY_EXISTS", f"Skill {name} already exists")
        self._ingest(skill_id, kwargs["config"]["local_path"])
        self.stored[name] = SimpleNamespace(
            name=name,
            display_name=kwargs["display_name"],
            description=kwargs["description"],
        )
        return self.stored[name]

    def get(self, **kwargs):
        self._record("get", kwargs)
        name = kwargs["name"]
        if name not in self.stored:
            raise _api_error(404, "NOT_FOUND", f"Skill {name} not found")
        return self.stored[name]

    def update(self, **kwargs):
        self._record("update", kwargs, "UpdateSkillConfig")
        name = kwargs["name"]
        skill_id = self._skill_id_of(name)
        if skill_id in self.raise_for_skill:
            raise self.raise_for_skill[skill_id]
        if name not in self.stored:
            raise _api_error(404, "NOT_FOUND", f"Skill {name} not found")
        config = kwargs["config"]
        if config.get("local_path"):
            self._ingest(skill_id, config["local_path"])
        stored = self.stored[name]
        stored.display_name = config.get("display_name", stored.display_name)
        stored.description = config.get("description", stored.description)
        return stored

    def delete(self, **kwargs):
        self._record("delete", kwargs)
        name = kwargs["name"]
        if name not in self.stored:
            raise _api_error(404, "NOT_FOUND", f"Skill {name} not found")
        del self.stored[name]
        return SimpleNamespace(name=name, done=True)

    def list(self, **kwargs):
        self._record("list", kwargs, "ListSkillsConfig")
        return iter(list(self.stored.values()))

    def retrieve(self, **kwargs):
        self._record("retrieve", kwargs, "RetrieveSkillsConfig")
        query = kwargs["query"].lower()
        hits = [
            SimpleNamespace(skill_name=s.name, description=s.description)
            for s in self.stored.values()
            if any(word in s.description.lower() for word in query.split())
        ]
        top_k = (kwargs.get("config") or {}).get("top_k")
        return SimpleNamespace(retrieved_skills=hits[:top_k] if top_k else hits)


class _FakeClient:
    def __init__(self, skills: _FakeSkillsApi | None = None) -> None:
        self.skills = skills or _FakeSkillsApi()


@pytest.fixture
def api() -> _FakeSkillsApi:
    return _FakeSkillsApi()


@pytest.fixture
def client(api: _FakeSkillsApi) -> _FakeClient:
    return _FakeClient(api)


class TestSkillResourceName:
    def test_bare_id_becomes_a_full_resource_path(self):
        from src.config import GCP_PROJECT_ID, GCP_REGION

        assert publisher.skill_resource_name("receipt-audit") == (
            f"projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/skills/receipt-audit"
        )

    def test_a_full_resource_name_passes_through(self):
        name = "projects/other/locations/europe-west4/skills/receipt-audit"
        assert publisher.skill_resource_name(name) == name

    def test_collection_relative_name_is_normalised(self):
        # `client.skills.list()` items and a hand-typed `skills/<id>` both have to
        # round-trip to the same absolute name, or update/delete target nothing.
        assert publisher.skill_resource_name("skills/receipt-audit") == (
            publisher.skill_resource_name("receipt-audit")
        )


class TestPublish:
    def test_publishes_every_skill(self, client, api):
        results = publisher.publish_skills(client=client)

        assert [r.action for r in results] == [publisher.ACTION_CREATED] * len(SKILL_DEFINITIONS)
        assert len(api.stored) == len(SKILL_DEFINITIONS)
        for definition in SKILL_DEFINITIONS:
            stored = api.stored[publisher.skill_resource_name(definition.skill_id)]
            assert stored.display_name == definition.display_name
            assert stored.description == definition.description
            # The uploaded bytes are the in-repo SKILL.md, not a re-derived copy.
            assert api.payloads[definition.skill_id] == definition.skill_md

    def test_create_kwargs_are_exactly_the_sdk_contract(self, client, api):
        publisher.publish_skills(client=client)
        method, kwargs = api.calls[1]
        assert method == "create"
        # Every REQUIRED parameter of the real signature is supplied by name.
        required = {
            name
            for name, p in _real_skills_signature("create").parameters.items()
            if p.default is inspect.Parameter.empty
        }
        assert required == {"skill_id", "display_name", "description"}
        assert required <= set(kwargs)
        assert set(kwargs["config"]) == {"local_path"}

    def test_republishing_updates_in_place(self, client, api):
        publisher.publish_skills(client=client)
        first_calls = len(api.calls)

        results = publisher.publish_skills(client=client)

        # The property that matters: one registry entry per skill, not two.
        assert len(api.stored) == len(SKILL_DEFINITIONS)
        assert [r.action for r in results] == [publisher.ACTION_UPDATED] * len(SKILL_DEFINITIONS)
        second = [method for method, _ in api.calls[first_calls:]]
        assert "create" not in second, "a re-publish must not re-create an existing skill"
        assert second.count("update") == len(SKILL_DEFINITIONS)

    def test_update_reuploads_the_current_skill_md(self, client, api):
        publisher.publish_skills(client=client)
        api.payloads.clear()
        publisher.publish_skills(client=client)
        # An update that forgot local_path would leave the registry serving the
        # first revision's instructions forever.
        assert api.payloads == {d.skill_id: d.skill_md for d in SKILL_DEFINITIONS}

    def test_lookup_precedes_create(self, client, api):
        """Red-state proof for idempotency.

        The fake raises ALREADY_EXISTS on a create against a stored id, exactly as
        the API does. Delete the get-then-update branch in `_publish_one` and this
        test goes from 3 updated to 3 failed.
        """
        publisher.publish_skills(client=client)
        api.calls.clear()

        results = publisher.publish_skills(client=client)

        assert all(r.action == publisher.ACTION_UPDATED for r in results)
        assert api.calls[0][0] == "get", "the existence probe must come before the write"

    def test_dry_run_touches_nothing(self, client, api):
        results = publisher.publish_skills(client=client, dry_run=True)
        assert [r.action for r in results] == [publisher.ACTION_DRY_RUN] * len(SKILL_DEFINITIONS)
        assert api.calls == []
        assert api.stored == {}

    def test_dry_run_does_not_even_construct_a_client(self, monkeypatch):
        # "Makes no calls" has to include the client itself: constructing it runs
        # ADC discovery and, on a GCE/Cloud Run host, hits the metadata server.
        def _boom():
            raise AssertionError("--dry-run must not touch credentials or the network")

        monkeypatch.setattr(publisher, "build_client", _boom)
        assert publisher.main(["--dry-run"]) == 0
        assert publisher.main(["--delete", "receipt-audit", "--dry-run"]) == 0

    def test_publishes_a_caller_supplied_subset(self, client, api):
        only = (SKILL_DEFINITIONS[0],)
        results = publisher.publish_skills(only, client=client)
        assert [r.skill_id for r in results] == [only[0].skill_id]
        assert len(api.stored) == 1


class TestFailurePosture:
    """Outcome 1 (preview absent) vs outcome 2 (a real failure) vs outcome 3."""

    def test_outcome_3_success_exits_zero_and_reports_n_of_m(self, client, monkeypatch, caplog):
        monkeypatch.setattr(publisher, "build_client", lambda: client)
        with caplog.at_level(logging.INFO):
            rc = publisher.main([])
        assert rc == 0
        assert f"{len(SKILL_DEFINITIONS)}/{len(SKILL_DEFINITIONS)}" in caplog.text

    def test_outcome_1_missing_collection_is_a_skip(self, client, api, monkeypatch, caplog):
        # A 404 on the collection-level create: the skills surface is not served
        # in this project/region.
        api.raise_on["create"] = _api_error(404, "NOT_FOUND", "Method not found.")
        results = publisher.publish_skills(client=client)
        assert [r.action for r in results] == [publisher.ACTION_SKIPPED] * len(SKILL_DEFINITIONS)

        monkeypatch.setattr(publisher, "build_client", lambda: client)
        with caplog.at_level(logging.INFO):
            assert publisher.main([]) == 0
        assert publisher.SKILL_REGISTRY_SKIP in caplog.text

    def test_outcome_1_service_disabled_403_is_a_skip(self, client, api):
        api.raise_on["create"] = _api_error(
            403,
            "PERMISSION_DENIED",
            "Vertex AI API has not been used in project 1234 before or it is disabled.",
        )
        results = publisher.publish_skills(client=client)
        assert all(r.action == publisher.ACTION_SKIPPED for r in results)

    def test_outcome_1_sdk_without_a_skills_surface_is_a_skip(self, monkeypatch, caplog):
        monkeypatch.setattr(publisher, "build_client", lambda: SimpleNamespace())
        with caplog.at_level(logging.INFO):
            assert publisher.main([]) == 0
        assert publisher.SKILL_REGISTRY_SKIP in caplog.text

    def test_outcome_2_bad_payload_exits_non_zero(self, client, api, monkeypatch, caplog):
        api.raise_on["create"] = _api_error(400, "INVALID_ARGUMENT", "description is required")
        results = publisher.publish_skills(client=client)
        assert [r.action for r in results] == [publisher.ACTION_FAILED] * len(SKILL_DEFINITIONS)

        monkeypatch.setattr(publisher, "build_client", lambda: client)
        with caplog.at_level(logging.INFO):
            assert publisher.main([]) == 1
        assert publisher.SKILL_REGISTRY_SKIP not in caplog.text

    def test_outcome_2_plain_permission_denied_is_a_failure_not_a_skip(self, client, api):
        # An IAM denial means the surface exists and refused *us*. Reading that as
        # "preview not enabled" is precisely the collapse this module must avoid.
        api.raise_on["create"] = _api_error(
            403, "PERMISSION_DENIED", "Permission 'aiplatform.skills.create' denied on resource"
        )
        results = publisher.publish_skills(client=client)
        assert all(r.action == publisher.ACTION_FAILED for r in results)

    def test_outcome_2_quota_and_server_errors_are_failures(self, client, api):
        for error in (
            _api_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded"),
            _api_error(503, "UNAVAILABLE", "backend unavailable"),
        ):
            api.raise_on["create"] = error
            results = publisher.publish_skills(client=client)
            assert all(r.action == publisher.ACTION_FAILED for r in results), error

    def test_a_partial_failure_still_publishes_the_rest_and_exits_non_zero(
        self, client, api, monkeypatch, caplog
    ):
        broken = SKILL_DEFINITIONS[1].skill_id
        api.raise_for_skill[broken] = _api_error(400, "INVALID_ARGUMENT", "nope")
        monkeypatch.setattr(publisher, "build_client", lambda: client)

        with caplog.at_level(logging.INFO):
            rc = publisher.main([])

        assert rc == 1
        assert len(api.stored) == len(SKILL_DEFINITIONS) - 1
        assert f"{len(SKILL_DEFINITIONS) - 1}/{len(SKILL_DEFINITIONS)}" in caplog.text
        assert broken in caplog.text

    def test_an_update_failure_is_reported(self, client, api):
        publisher.publish_skills(client=client)
        api.raise_on["update"] = _api_error(400, "INVALID_ARGUMENT", "bad update mask")
        results = publisher.publish_skills(client=client)
        assert all(r.action == publisher.ACTION_FAILED for r in results)

    def test_a_403_on_the_existence_probe_is_a_failure(self, client, api):
        # `get` 404 means "no such skill" and must stay a normal create; anything
        # else on the probe is a real failure and must not be swallowed.
        api.raise_on["get"] = _api_error(403, "PERMISSION_DENIED", "denied on resource")
        results = publisher.publish_skills(client=client)
        assert all(r.action == publisher.ACTION_FAILED for r in results)


class TestListSearchDelete:
    def test_list_returns_the_registered_skills(self, client, api):
        publisher.publish_skills(client=client)
        listed = publisher.list_skills(client=client)
        assert {s.name for s in listed} == set(api.stored)

    def test_list_cli_prints_names_and_exits_zero(self, client, monkeypatch, capsys):
        publisher.publish_skills(client=client)
        monkeypatch.setattr(publisher, "build_client", lambda: client)
        assert publisher.main(["--list"]) == 0
        out = capsys.readouterr().out
        for definition in SKILL_DEFINITIONS:
            assert definition.skill_id in out

    def test_list_degrades_when_the_surface_is_absent(self, client, api, monkeypatch, caplog):
        api.raise_on["list"] = _api_error(404, "NOT_FOUND", "Method not found.")
        monkeypatch.setattr(publisher, "build_client", lambda: client)
        with caplog.at_level(logging.INFO):
            assert publisher.main(["--list"]) == 0
        assert publisher.SKILL_REGISTRY_SKIP in caplog.text

    def test_list_reports_a_real_failure(self, client, api, monkeypatch):
        api.raise_on["list"] = _api_error(403, "PERMISSION_DENIED", "denied on resource")
        monkeypatch.setattr(publisher, "build_client", lambda: client)
        assert publisher.main(["--list"]) == 1

    def test_search_uses_the_semantic_retrieve_surface(self, client, api):
        publisher.publish_skills(client=client)
        hits = publisher.search_skills("receipt", client=client)
        assert [method for method, _ in api.calls if method == "retrieve"] == ["retrieve"]
        assert any("receipt-audit" in hit.skill_name for hit in hits)

    def test_search_passes_top_k_through_the_real_config_field(self, client, api):
        publisher.publish_skills(client=client)
        publisher.search_skills("expense receipt booking", client=client, top_k=1)
        _, kwargs = next((m, k) for m, k in api.calls if m == "retrieve")
        assert kwargs["query"] == "expense receipt booking"
        assert kwargs["config"] == {"top_k": 1}

    def test_search_cli_exits_zero_when_nothing_matches(self, client, monkeypatch, caplog):
        publisher.publish_skills(client=client)
        monkeypatch.setattr(publisher, "build_client", lambda: client)
        with caplog.at_level(logging.INFO):
            assert publisher.main(["--search", "zzzz-nothing-matches"]) == 0

    def test_search_degrades_when_the_surface_is_absent(self, client, api, monkeypatch, caplog):
        api.raise_on["retrieve"] = _api_error(404, "NOT_FOUND", "Method not found.")
        monkeypatch.setattr(publisher, "build_client", lambda: client)
        with caplog.at_level(logging.INFO):
            assert publisher.main(["--search", "receipt"]) == 0
        assert publisher.SKILL_REGISTRY_SKIP in caplog.text

    def test_delete_removes_the_skill(self, client, api, monkeypatch):
        publisher.publish_skills(client=client)
        target = SKILL_DEFINITIONS[0].skill_id
        monkeypatch.setattr(publisher, "build_client", lambda: client)

        assert publisher.main(["--delete", target]) == 0

        assert publisher.skill_resource_name(target) not in api.stored
        assert len(api.stored) == len(SKILL_DEFINITIONS) - 1
        _, kwargs = next((m, k) for m, k in api.calls if m == "delete")
        assert kwargs["name"] == publisher.skill_resource_name(target)

    def test_delete_dry_run_touches_nothing(self, client, api, monkeypatch):
        publisher.publish_skills(client=client)
        api.calls.clear()
        monkeypatch.setattr(publisher, "build_client", lambda: client)

        assert publisher.main(["--delete", SKILL_DEFINITIONS[0].skill_id, "--dry-run"]) == 0

        assert len(api.stored) == len(SKILL_DEFINITIONS)
        assert "delete" not in [method for method, _ in api.calls]

    def test_deleting_an_unknown_skill_is_a_failure_not_a_skip(self, client, monkeypatch, caplog):
        # A 404 here is about the *id*, not the API: the collection probe proves
        # the surface is alive, so this must read red rather than "preview off".
        monkeypatch.setattr(publisher, "build_client", lambda: client)
        with caplog.at_level(logging.INFO):
            assert publisher.main(["--delete", "no-such-skill"]) == 1
        assert publisher.SKILL_REGISTRY_SKIP not in caplog.text

    def test_delete_degrades_when_the_surface_is_absent(self, client, api, monkeypatch, caplog):
        api.raise_on["get"] = _api_error(404, "NOT_FOUND", "Method not found.")
        api.raise_on["list"] = _api_error(404, "NOT_FOUND", "Method not found.")
        monkeypatch.setattr(publisher, "build_client", lambda: client)
        with caplog.at_level(logging.INFO):
            assert publisher.main(["--delete", "receipt-audit"]) == 0
        assert publisher.SKILL_REGISTRY_SKIP in caplog.text


class TestClientConstruction:
    def test_builds_an_agentplatform_client_not_a_vertexai_one(self, monkeypatch):
        # The two are separate module copies; `vertexai.Client` also FutureWarns.
        # See docs/notes/agentplatform-client-migration.md.
        captured = {}

        def _fake_client(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(skills=object())

        import agentplatform

        monkeypatch.setattr(agentplatform, "Client", _fake_client)
        publisher.build_client()

        from src.config import GCP_PROJECT_ID, GCP_REGION

        assert captured == {"project": GCP_PROJECT_ID, "location": GCP_REGION}

    def test_missing_credentials_degrade_to_a_skip(self, monkeypatch, caplog):
        from google.auth import exceptions as auth_exceptions

        def _no_adc():
            raise auth_exceptions.DefaultCredentialsError("no ADC")

        monkeypatch.setattr(publisher, "build_client", _no_adc)
        with caplog.at_level(logging.INFO):
            assert publisher.main([]) == 0
        assert publisher.SKILL_REGISTRY_SKIP in caplog.text
