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
  of the MCP servers (a skill that references an invented tool is worse than no
  skill: it teaches a procedure the agent cannot execute);
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
