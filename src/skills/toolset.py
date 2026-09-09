"""Runtime skill discovery — the coordinator's read half of the Skill Registry.

:mod:`src.skills.publish_skills` is the write half: it uploads the definitions in
:mod:`src.skills.definitions` to the registry. This module is what an agent uses
at run time. :func:`get_skill_toolset` returns an ADK ``SkillToolset`` backed by
``GCPSkillRegistry``, which gives the model ``search_skills`` / ``load_skill`` /
``load_skill_resource`` — so the procedure lives in the registry and is fetched
on demand, rather than being pasted into the agent's instruction.

**No ``code_executor``, deliberately.** ``SkillToolset`` accepts one (and an
``environment``) to run a skill's ``scripts/``; our skills are instruction-only
by design (see :mod:`src.skills.definitions`), so wiring an executor would add a
sandbox and an arbitrary-code-execution surface for no demo value.

**Failure posture: degrade, loudly.** Modelled on
``src.registry.get_mcp_tools`` — any failure returns ``None`` with a **WARNING**
naming the underlying error, the project, and the location, because an agent
quietly running without the skills it is supposed to have is exactly the silent
degradation that has to be visible. It never raises: the coordinator builds its
tool list at *import* time, so an exception here would take the whole agent down
at container start over an optional, opt-in enhancement.

Wired into the coordinator behind ``ENABLE_SKILL_REGISTRY`` (default off — see
``src/config.py``).
"""

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from src.config import GCP_PROJECT_ID, SKILL_REGISTRY_LOCATION

if TYPE_CHECKING:  # annotations only — the runtime imports are deliberately lazy
    from google.adk.tools.skill_toolset import SkillToolset

log = logging.getLogger(__name__)


def _gcp_skill_registry(project_id: str, location: str) -> Any:
    """Construct ADK's GCP Skill Registry client.

    A named factory rather than an inline import so callers (and tests) have a
    seam: it is the one line that touches the preview SDK surface. Constructing
    it performs no I/O — it validates its arguments and reads env; credentials
    are resolved lazily on the first request.
    """
    from google.adk.integrations.skill_registry import GCPSkillRegistry

    return GCPSkillRegistry(project_id=project_id, location=location)


def get_skill_toolset(
    *,
    project_id: str = GCP_PROJECT_ID,
    location: str = SKILL_REGISTRY_LOCATION,
    registry_factory: Callable[[str, str], Any] | None = None,
) -> "SkillToolset | None":
    """A ``SkillToolset`` over the Skill Registry, or ``None`` (logged) on failure.

    ``location`` defaults to :data:`src.config.SKILL_REGISTRY_LOCATION` — the same
    constant the publisher writes with, so the agent reads where the skills were
    written.
    """
    factory = registry_factory or _gcp_skill_registry
    try:
        # Imported here, not at module scope, for two reasons: the Skill Registry
        # is a preview surface, so an ADK without it must degrade to a WARNING
        # rather than make this module unimportable (and with it the coordinator);
        # and a default deploy with the flag off should not pay for the import.
        from google.adk.tools.skill_toolset import SkillToolset

        registry = factory(project_id, location)
        # code_executor / environment intentionally unset — instruction-only
        # skills; see the module docstring.
        toolset = SkillToolset(registry=registry)
    except Exception as exc:
        # Broad, unlike src.registry.get_mcp_tools' (RuntimeError, ValueError),
        # and the reason is worth stating. The known construction-time failures
        # already span three types — ImportError (ADK too old for the preview),
        # ValueError (GCPSkillRegistry rejects a missing project/location,
        # SkillToolset rejects a bad skills/environment combination), and
        # RuntimeError — none of which is a stable contract on a preview SDK. The
        # cost of guessing that list wrong is not a fallback path, as it is for
        # MCP: it is the coordinator failing to import inside the container, over
        # a feature that is off by default. So this takes register_a2a_agent's
        # preview-optional posture instead, and pays for it with a WARNING that
        # names the error rather than an INFO-level skip.
        log.warning(
            "Skill Registry toolset unavailable (project=%s, location=%s): %s — "
            "the agent will run without skill discovery",
            project_id,
            location,
            exc,
        )
        return None

    log.info("Skill Registry toolset wired (project=%s, location=%s)", project_id, location)
    return toolset
