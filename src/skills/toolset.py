"""Runtime skill discovery — the coordinator's read half of the Skill Registry.

:mod:`src.skills.publish_skills` is the write half: it uploads the definitions in
:mod:`src.skills.definitions` to the registry. This module is what an agent uses
at run time. :func:`get_skill_toolset` returns an ADK ``SkillToolset`` backed by
``GCPSkillRegistry``, which gives the model ``search_skills`` / ``list_skills`` /
``load_skill`` / ``load_skill_resource`` — so the procedure lives in the registry
and is fetched on demand, rather than being pasted into the agent's instruction.

**No ``code_executor``, deliberately — and no ``run_skill_script`` either.**
``SkillToolset`` accepts a ``code_executor`` (or an ``environment``) to run a
skill's ``scripts/``; our skills are instruction-only by design (see
:mod:`src.skills.definitions`), so wiring one would add a sandbox and an
arbitrary-code-execution surface for no demo value. ADK builds
``RunSkillScriptTool`` unconditionally regardless, and it declares itself to the
model either way, so leaving the executor unset would expose a fifth callable
tool that can only ever return ``SCRIPT_NOT_FOUND``/``NO_CODE_EXECUTOR``.
``tool_filter`` removes it: the coordinator's tool surface is an input to
``tool_use_accuracy``, so a tool that cannot succeed is a measurable cost.

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
    from google.adk.integrations.skill_registry import GCPSkillRegistry
    from google.adk.tools.skill_toolset import SkillToolset

log = logging.getLogger(__name__)

# The four tools worth exposing. See the module docstring for why
# ``run_skill_script`` — ADK's fifth, built unconditionally — is filtered out.
# A ``list``, not a tuple: ADK's ``BaseToolset._is_tool_selected`` matches names
# only for ``isinstance(tool_filter, list)`` and otherwise falls through to
# ``return False``, so a tuple here would silently expose NO tools at all.
SKILL_TOOL_NAMES = ["list_skills", "load_skill", "load_skill_resource", "search_skills"]


def _gcp_skill_registry(project_id: str, location: str) -> "GCPSkillRegistry":
    """Construct ADK's GCP Skill Registry client.

    A named factory rather than an inline import so callers (and tests) have a
    seam: it is the one line that touches the preview SDK surface. Construction
    is nearly free — it validates its arguments and reads env (credentials are
    resolved lazily on the first request) — but not literally I/O-free: when a
    default client cert source exists it writes two tempfiles and loads a cert
    chain to build an mTLS ``SSLContext``.
    """
    from google.adk.integrations.skill_registry import GCPSkillRegistry

    return GCPSkillRegistry(project_id=project_id, location=location)


def get_skill_toolset(
    *,
    project_id: str = GCP_PROJECT_ID,
    location: str = SKILL_REGISTRY_LOCATION,
    registry_factory: Callable[..., Any] | None = None,
) -> "SkillToolset | None":
    """A ``SkillToolset`` over the Skill Registry, or ``None`` (logged) on failure.

    ``location`` defaults to :data:`src.config.SKILL_REGISTRY_LOCATION`, the one
    constant both halves of the integration share (see ``src/config.py`` for why).

    ``registry_factory`` is called with **keyword** arguments so that
    ``GCPSkillRegistry`` itself — whose ``__init__`` is keyword-only — is a valid
    factory. Passed positionally it raised a ``TypeError`` that the handler below
    then reported as "registry unavailable", turning our own wrong call shape into
    a plausible-looking infrastructure message.
    """
    factory = registry_factory or _gcp_skill_registry
    try:
        # Imported here, not at module scope, for two reasons: the Skill Registry
        # is a preview surface, so an ADK without it must degrade to a WARNING
        # rather than make this module unimportable (and with it the coordinator);
        # and a default deploy with the flag off should not pay for the import.
        from google.adk.tools.skill_toolset import SkillToolset

        registry = factory(project_id=project_id, location=location)
        # code_executor / environment intentionally unset, and run_skill_script
        # filtered out with them — instruction-only skills; see the module
        # docstring.
        toolset = SkillToolset(registry=registry, tool_filter=SKILL_TOOL_NAMES)
    except Exception as exc:
        # Broad, unlike src.registry.get_mcp_tools' (RuntimeError, ValueError),
        # and the reason is worth stating. Two construction-time failure types are
        # known today — ImportError (ADK too old for the preview) and ValueError
        # (GCPSkillRegistry rejects a missing project/location, SkillToolset
        # rejects a bad skills/environment combination) — on a preview SDK whose
        # error contract is not stable. The cost of guessing that list wrong is
        # not a fallback path, as it is for MCP: it is the coordinator failing to
        # import inside the container, over a feature that is off by default. So
        # this takes register_a2a_agent's preview-optional posture instead, and
        # pays for it by naming the exception TYPE — which is the whole diagnostic
        # value a broad handler gives away otherwise (ImportError = ADK too old,
        # TypeError = our call shape, ValueError = bad config).
        log.warning(
            "Skill Registry toolset unavailable (project=%s, location=%s): %s: %s — "
            "the agent will run without skill discovery; "
            "`python -m src.skills.publish_skills --list` shows what the registry holds",
            project_id,
            location,
            type(exc).__name__,
            exc,
        )
        return None

    log.info("Skill Registry toolset wired (project=%s, location=%s)", project_id, location)
    return toolset
