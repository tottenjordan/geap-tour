"""In-repo skill definitions for the Gemini Enterprise Skill Registry.

The registry is a deployment target; the `SKILL.md` content is source, and lives
in :mod:`src.skills.definitions` so it is reviewable and diffable. Skills here
are instruction-only — no executable scripts. See that module's docstring.
"""

from src.skills.definitions import (
    EXPENSE_POLICY_TRIAGE,
    RECEIPT_AUDIT,
    SKILL_DEFINITIONS,
    SKILL_MD_FILENAME,
    TRIP_PLANNING_BRIEF,
    SkillDefinition,
    get_skill,
    materialize_skill,
)

__all__ = [
    "EXPENSE_POLICY_TRIAGE",
    "RECEIPT_AUDIT",
    "SKILL_DEFINITIONS",
    "SKILL_MD_FILENAME",
    "TRIP_PLANNING_BRIEF",
    "SkillDefinition",
    "get_skill",
    "materialize_skill",
]
