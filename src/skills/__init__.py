"""In-repo skill definitions for the Gemini Enterprise Skill Registry.

The registry is a deployment target; the `SKILL.md` content is source, and lives
in :mod:`src.skills.definitions` so it is reviewable and diffable. Skills here
are instruction-only — no executable scripts. See that module's docstring.

Only the definitions are re-exported. The publisher lives in
:mod:`src.skills.publish_skills` and is imported from there on purpose: its main
entry point is a function also called ``publish_skills``, so re-exporting it
would rebind ``src.skills.publish_skills`` from the submodule to the function
once this package is imported.
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
