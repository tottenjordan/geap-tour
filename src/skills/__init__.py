"""In-repo skill definitions for the Gemini Enterprise Skill Registry.

The registry is a deployment target; the `SKILL.md` content is source, and lives
in :mod:`src.skills.definitions` so it is reviewable and diffable. Skills here
are instruction-only — no executable scripts. See that module's docstring.

Only the definitions are re-exported — this package's public face is the source
of truth, not the two things that act on it. The publisher lives in
:mod:`src.skills.publish_skills` and is imported from there on purpose: its main
entry point is a function also called ``publish_skills``, so re-exporting it
would rebind ``src.skills.publish_skills`` from the submodule to the function
once this package is imported. :func:`src.skills.toolset.get_skill_toolset` is
omitted for a plainer reason — no name collision applies, it is a scope choice:
the two halves that *act* on the definitions, the publisher (write) and the
runtime toolset (read), are each imported from the module whose docstring
documents that half's own failure posture, so there is exactly one place to read
before calling either. Its one caller, the coordinator, imports it from
:mod:`src.skills.toolset` directly.
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
