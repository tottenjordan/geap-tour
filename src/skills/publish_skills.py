"""Publish / inspect this repo's skills in the Gemini Enterprise Skill Registry.

The definitions in :mod:`src.skills.definitions` are the source of truth; this
module is the only thing that talks to the registry. It is modelled on
``src/deploy/register_a2a.py`` — argparse, mutually-exclusive subcommands, a
``main(argv) -> int`` — and adds the two things the intro notebook does not do.

**Idempotency.** Re-publishing an existing ``skill_id`` updates in place. The
notebook creates unconditionally under a timestamped id, which is fine for a
scratch demo and wrong for a checked-in skill set: the registry would accumulate
a new copy of every skill on every run, semantic retrieval would score the
request against N near-identical descriptions, and an agent could load a stale
revision. This repo has already paid for that lesson once —
``quality_alerts.create_quality_alert`` had to be changed to update in place
after duplicate alert policies piled up. So every publish does a
``skills.get`` first and takes the ``skills.update`` branch when the id is
already there.

**A failure that is not a skip.** Two very different things surface as an
exception out of ``client.skills``, and collapsing them is the bug this module
is written to avoid:

* the Skill Registry *surface* is not available here — the installed SDK is too
  old to expose ``client.skills``, the preview endpoint is not served in this
  project/region, the API is switched off, or there are no credentials at all.
  Nothing was asked of the registry that it could have done, so this is a logged
  skip and **exit 0**, exactly like ``register_a2a``: a live demo must not die
  because a preview surface is not enabled.
* a call the registry understood and *refused* — bad payload, IAM denial, quota,
  a 5xx. The surface is there; our request failed. That is **exit non-zero**.

:func:`_is_registry_unavailable` is where that judgement lives, and it decides on
transport-level facts (an ``APIError`` code, a named service-disabled message)
rather than by catching ``Exception`` and hoping. Because a bare 404 is ambiguous
— "this method is not served here" and "the thing you addressed is gone" arrive
identically — :func:`_is_skip` is the only entry point callers use, and it
refuses to read a 404 as *absent* whenever the surface has already been shown to
answer: after a successful ``get`` (so a 404 from the following ``update`` is a
real failure, not a skip), or when a cheap collection probe succeeds.

Usage::

    uv run python -m src.skills.publish_skills                  # publish (default)
    uv run python -m src.skills.publish_skills --dry-run        # plan only, no calls
    uv run python -m src.skills.publish_skills --list
    uv run python -m src.skills.publish_skills --search "split a receipt by category"
    uv run python -m src.skills.publish_skills --delete receipt-audit
"""

import argparse
import logging
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

# SKILL_REGISTRY_LOCATION, never GCP_REGION: the publisher writes skills to the
# location the coordinator's toolset (src/skills/toolset.py) reads them from,
# and the only way to keep those two from drifting apart — a drift with no
# local symptom, since both sides pass every offline test either way — is for
# both to read one constant. It defaults to GCP_REGION, so the default
# behaviour is unchanged; an override moves both halves at once.
from src.config import GCP_PROJECT_ID, SKILL_REGISTRY_LOCATION
from src.skills.definitions import SKILL_DEFINITIONS, SkillDefinition, materialize_skill

if TYPE_CHECKING:  # import cost + the SDK is optional at runtime; annotations only
    from agentplatform import Client
    from agentplatform._genai.skills import Skills

log = logging.getLogger("publish_skills")

# The one string that means "the surface isn't here" — greppable, and the thing
# the CLI tests assert on to tell a skip apart from a failure.
SKILL_REGISTRY_SKIP = "Skill Registry preview not enabled — skipping"

ACTION_CREATED = "created"
ACTION_UPDATED = "updated"
ACTION_DELETED = "deleted"
ACTION_DRY_RUN = "dry-run"
ACTION_FAILED = "failed"
ACTION_SKIPPED = "skipped"

_SUCCESSFUL_ACTIONS = frozenset({ACTION_CREATED, ACTION_UPDATED, ACTION_DELETED, ACTION_DRY_RUN})

# Substrings Google's APIs use when the *service* is off rather than the caller
# being unauthorized. A 403 carrying one of these is an unconfigured project, not
# a permissions bug in our deployment.
_SERVICE_DISABLED_MARKERS = (
    "SERVICE_DISABLED",
    "accessNotConfigured",
    "has not been used in project",
    "is not enabled",
    "is disabled",
)


class SkillRegistryUnavailable(RuntimeError):
    """The Skill Registry surface is absent here.

    Distinct from "the call failed": this is raised only when there is nothing to
    call — no ``skills`` attribute on the client, no ``agentplatform``, no
    credentials — so it always maps to a skip, never to a red exit.
    """


def _skip_message(project: str = GCP_PROJECT_ID, location: str = SKILL_REGISTRY_LOCATION) -> str:
    """The skip line, always naming what it tried to reach.

    "preview not enabled" mid-demo begs one question — *which project and
    region?* — and :func:`build_client` defaults both silently, so a skip caused
    by a mis-set location looks identical to one caused by a genuinely
    unserved preview unless the message says.
    """
    return f"{SKILL_REGISTRY_SKIP} (project={project}, location={location})"


@dataclass(frozen=True)
class SkillActionResult:
    """What happened to one skill. ``action`` is one of the ``ACTION_*`` constants.

    Covers every registry action, not just publishing: :func:`delete_skill`
    returns one too, with ``ACTION_DELETED``.
    """

    skill_id: str
    action: str
    resource_name: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.action in _SUCCESSFUL_ACTIONS


def build_client(
    project: str = GCP_PROJECT_ID, location: str = SKILL_REGISTRY_LOCATION
) -> "Client":
    """Construct the Agent Platform client the Skill Registry lives on.

    ``agentplatform.Client``, never ``vertexai.Client``: they are separate module
    copies (``agentplatform.types is vertexai.types`` -> False), the vertexai one
    FutureWarns, and mixing them silently loses this repo's SDK patches. See
    ``docs/notes/agentplatform-client-migration.md`` and
    ``tests/test_agentplatform_client.py``.
    """
    try:
        import agentplatform
    except ImportError as exc:  # SDK not installed / too old to ship agentplatform
        raise SkillRegistryUnavailable(f"agentplatform is not importable: {exc}") from exc
    return agentplatform.Client(project=project, location=location)


def skill_resource_name(
    skill_id: str, *, project: str = GCP_PROJECT_ID, location: str = SKILL_REGISTRY_LOCATION
) -> str:
    """Absolute resource name for a skill id.

    ``skills.get``/``update``/``delete`` take ``name``, documented as
    ``projects/{project}/locations/{location}/skills/{skill}``. The underlying
    google-genai transport only prefixes ``projects/{p}/locations/{l}/`` onto a
    path that does not already start with ``projects/``, so a bare id would be
    pasted straight into the URL and address nothing. Always send the absolute
    form; accept an absolute one (e.g. straight off ``skills.list()``) unchanged.
    """
    if skill_id.startswith("projects/"):
        return skill_id
    return f"projects/{project}/locations/{location}/skills/{skill_id.rsplit('/', 1)[-1]}"


def _skills_api(client: "Client") -> "Skills":
    """The ``skills`` sub-client, or a skip-shaped error if the SDK has none.

    Checked explicitly rather than letting an ``AttributeError`` propagate into
    the classifier: a typo in *our* attribute access is also an AttributeError,
    and it must not be able to masquerade as "preview not enabled".
    """
    api = getattr(client, "skills", None)
    if api is None:
        raise SkillRegistryUnavailable(
            "the installed google-cloud-aiplatform exposes no client.skills surface"
        )
    return api


def _api_code(exc: BaseException) -> int | None:
    """HTTP status of a google-genai ``APIError``, or None if it isn't one."""
    from google.genai import errors

    if isinstance(exc, errors.APIError):
        code = getattr(exc, "code", None)
        return code if isinstance(code, int) else None
    return None


def _is_registry_unavailable(exc: BaseException) -> bool:
    """True when the *surface* is missing, false when a call was refused.

    Reads the exception alone, so it cannot tell the two meanings of a 404 apart
    and answers with the optimistic one. Never call it directly: :func:`_is_skip`
    is the entry point, and it supplies the evidence this function lacks. A 404
    from ``get``/``update``/``delete`` is about the skill id, not about the API,
    and routing one straight through here turns "no skill called X" — or a
    ``update`` racing a deletion — into a silent success.
    """
    if isinstance(exc, SkillRegistryUnavailable):
        return True

    from google.auth import exceptions as auth_exceptions

    if isinstance(exc, auth_exceptions.DefaultCredentialsError):
        # No ADC at all: the environment is not wired for GCP, which is the same
        # "nothing to call" class as a missing preview (register_a2a's docstring
        # names missing credentials as a skip too). A *wrong* identity still
        # reaches the API and comes back 403 -> a failure, below.
        return True

    code = _api_code(exc)
    if code in (404, 501):
        # UNIMPLEMENTED is unambiguous. NOT_FOUND is not — this is the optimistic
        # reading `_is_skip` then has to check against the collection.
        return True
    if code == 403:
        text = f"{getattr(exc, 'message', '')} {getattr(exc, 'details', '')}"
        return any(marker in text for marker in _SERVICE_DISABLED_MARKERS)
    return False


def _collection_answers(api: "Skills") -> bool:
    """Did the cheapest possible collection call succeed?

    A yes is proof the method is served in this project/location, which is what
    demotes an ambiguous 404 from "not enabled here" to "our request was
    refused". Only ever run on a path that has already failed, so the extra call
    costs nothing in the normal case.
    """
    try:
        list(api.list(config={"page_size": 1}))
    except Exception:
        return False
    return True


def _is_skip(
    exc: BaseException, api: "Skills | None" = None, *, surface_proven: bool = False
) -> bool:
    """Should ``exc`` be reported as a skip (exit 0) rather than a failure?

    This is where the 404 ambiguity is resolved with evidence rather than
    assumed away:

    * ``surface_proven`` — the caller has already had a call answered on this
      run (e.g. ``get`` returned the skill we are about to ``update``), so
      nothing that follows can mean "the API isn't here". A 404 then is a real
      failure: the skill was deleted underneath us, or we addressed the wrong
      name.
    * otherwise a 404 gets one cheap ``list`` probe. If the collection answers,
      the endpoint is served and our call was refused — red, not skipped.

    The residual case both of these cannot separate is a parent that does not
    exist at all (a mistyped project, a region without the preview): *every*
    call including the probe 404s, and the transport gives us nothing further to
    go on. That one stays a skip, which is why :func:`_skip_message` names the
    project and location it tried.
    """
    if isinstance(exc, SkillRegistryUnavailable):
        return True
    if surface_proven:
        return False
    if not _is_registry_unavailable(exc):
        return False
    if _api_code(exc) == 404 and api is not None:
        return not _collection_answers(api)
    return True


def _log_skip_or_failure(
    what: str, exc: BaseException, api: "Skills | None" = None, *, surface_proven: bool = False
) -> bool:
    """Log ``exc`` as a skip (returns True) or as a failure (returns False).

    One definition of the posture, so the six call sites cannot drift into
    disagreeing about which errors are survivable.
    """
    if _is_skip(exc, api, surface_proven=surface_proven):
        log.info("%s — could not %s: %s", _skip_message(), what, exc)
        return True
    log.error("FAILED to %s: %s", what, exc)
    return False


def _lookup(api: "Skills", name: str):
    """Return the registered skill at ``name``, or None if there isn't one.

    A 404 here is the ordinary "not published yet" answer and is the whole basis
    of idempotency. Anything else — a 403, a 5xx — is re-raised: swallowing it
    would send us down the create path and report a misleading second error.
    """
    try:
        return api.get(name=name)
    except Exception as exc:
        if _api_code(exc) == 404:
            return None
        raise


def _resource_name_of(result: object, fallback: str) -> str:
    """Prefer the server's own resource name, fall back to the one we addressed.

    ``create``/``update`` return a ``Skill`` while ``wait_for_completion``
    defaults True, but a ``SkillOperation`` otherwise — and an operation's
    ``name`` is the operation's, not the skill's. Only trust a name that looks
    like a skill resource.
    """
    name = getattr(result, "name", None)
    return name if isinstance(name, str) and "/skills/" in name else fallback


def _dry_run_result(skill: SkillDefinition) -> SkillActionResult:
    """What a publish *would* do. Takes no ``api`` because it needs none."""
    name = skill_resource_name(skill.skill_id)
    log.info("[dry-run] would publish %s -> %s", skill.skill_id, name)
    return SkillActionResult(skill.skill_id, ACTION_DRY_RUN, name)


def _publish_one(api: "Skills", skill: SkillDefinition) -> SkillActionResult:
    name = skill_resource_name(skill.skill_id)
    # Set the moment `get` answers with a skill: from then on the registry has
    # demonstrably served us, so a later 404 from `update` is a failure (the
    # skill was deleted between the two calls) and never "preview not enabled".
    surface_proven = False

    try:
        existing = _lookup(api, name)
        surface_proven = existing is not None
        # The create/update call MUST happen inside this `with`: materialize_skill
        # deletes the directory on exit and the SDK zips `local_path` at call time.
        with materialize_skill(skill) as skill_dir:
            local_path = str(skill_dir)
            if existing is None:
                result = api.create(
                    skill_id=skill.skill_id,
                    display_name=skill.display_name,
                    description=skill.description,
                    config={"local_path": local_path},
                )
                action = ACTION_CREATED
            else:
                # local_path is re-sent on every update: without it the registry
                # keeps serving the previous revision's SKILL.md, so an edited
                # instruction body would never reach the agent.
                result = api.update(
                    name=name,
                    config={
                        "local_path": local_path,
                        "display_name": skill.display_name,
                        "description": skill.description,
                    },
                )
                action = ACTION_UPDATED
    except Exception as exc:
        skipped = _log_skip_or_failure(
            f"publish {skill.skill_id}", exc, api, surface_proven=surface_proven
        )
        return SkillActionResult(
            skill.skill_id, ACTION_SKIPPED if skipped else ACTION_FAILED, name, str(exc)
        )

    resource = _resource_name_of(result, name)
    log.info("%s %s -> %s", action, skill.skill_id, resource)
    return SkillActionResult(skill.skill_id, action, resource)


def publish_skills(
    skills: Iterable[SkillDefinition] | None = None,
    *,
    client: "Client | None" = None,
    dry_run: bool = False,
) -> list[SkillActionResult]:
    """Publish (create-or-update) each skill; one result per skill, never raises.

    Per-skill classification rather than fail-fast: one bad skill should not stop
    the other two from reaching the registry, and the caller still learns that
    something failed from the returned actions.
    """
    definitions = tuple(SKILL_DEFINITIONS if skills is None else skills)
    if dry_run:
        return [_dry_run_result(s) for s in definitions]
    api = _skills_api(client if client is not None else build_client())
    return [_publish_one(api, s) for s in definitions]


def list_skills(*, client: "Client | None" = None) -> list:
    """Every skill registered in this project/location."""
    api = _skills_api(client if client is not None else build_client())
    return list(api.list())


def search_skills(query: str, *, client: "Client | None" = None) -> list:
    """Semantic search over the registry — the surface an agent uses at runtime.

    This is the check that matters before wiring a skill into an agent: a skill
    that is published but does not come back for a plausible user request is
    invisible to the retrieval the agent itself performs.
    """
    api = _skills_api(client if client is not None else build_client())
    # top_k is fixed and small on purpose: the question this answers is "does the
    # skill come back near the top for a plausible request", not "dump the
    # registry" (that is --list). Nothing has needed to vary it.
    response = api.retrieve(query=query, config={"top_k": 5})
    # Attribute access, NOT `getattr(..., None)`: if the SDK renames this field,
    # a default would make `--search` answer "0 matches" — a confident wrong
    # answer to the one question the subcommand exists to ask — where an
    # AttributeError surfaces as a red exit. `or []` still covers the field's
    # real, documented None (a response with no hits). Pinned by
    # tests/test_publish_skills.py::TestSdkResponseFields.
    return list(response.retrieved_skills or [])


def delete_skill(
    skill_id: str, *, client: "Client | None" = None, dry_run: bool = False
) -> SkillActionResult:
    """Delete one skill by id (or absolute resource name).

    On a miss this deliberately probes the collection before deciding: a 404 from
    ``delete`` alone cannot distinguish "no skill called X" (a real failure — the
    user asked to remove something) from "no skills API here" (a skip). One cheap
    ``list`` settles it, and only on the miss path.
    """
    name = skill_resource_name(skill_id)
    if dry_run:
        log.info("[dry-run] would delete %s", name)
        return SkillActionResult(skill_id, ACTION_DRY_RUN, name)

    api = _skills_api(client if client is not None else build_client())
    what = f"delete {skill_id}"
    try:
        existing = _lookup(api, name)
    except Exception as exc:
        skipped = _log_skip_or_failure(what, exc, api)
        return SkillActionResult(
            skill_id, ACTION_SKIPPED if skipped else ACTION_FAILED, name, str(exc)
        )

    if existing is None:
        try:
            list(api.list(config={"page_size": 1}))
        except Exception as exc:
            skipped = _log_skip_or_failure(what, exc)
            return SkillActionResult(
                skill_id, ACTION_SKIPPED if skipped else ACTION_FAILED, name, str(exc)
            )
        log.error(
            "FAILED to delete %s: no such skill in the registry "
            "(project=%s, location=%s) — `--list` shows what is there",
            skill_id,
            GCP_PROJECT_ID,
            SKILL_REGISTRY_LOCATION,
        )
        return SkillActionResult(skill_id, ACTION_FAILED, name, "no such skill")

    try:
        api.delete(name=name)
    except Exception as exc:
        log.error("FAILED to delete %s: %s", skill_id, exc)
        return SkillActionResult(skill_id, ACTION_FAILED, name, str(exc))

    log.info("deleted %s", name)
    return SkillActionResult(skill_id, ACTION_DELETED, name)


def _run_publish(client: "Client | None", *, dry_run: bool) -> int:
    results = publish_skills(client=client, dry_run=dry_run)
    published = [r for r in results if r.ok]
    failed = [r for r in results if r.action == ACTION_FAILED]
    skipped = [r for r in results if r.action == ACTION_SKIPPED]

    verb = "Would publish" if dry_run else "Published"
    log.info("%s %d/%d skill(s).", verb, len(published), len(results))
    if skipped:
        log.info("%s — %d skill(s) not published", _skip_message(), len(skipped))
    for result in failed:
        log.error("  FAILED %s: %s", result.skill_id, result.error)
    return 1 if failed else 0


def _run_list(client: "Client | None") -> int:
    try:
        skills = list_skills(client=client)
    except Exception as exc:
        return 0 if _log_skip_or_failure("list skills", exc) else 1
    if not skills:
        log.info(
            "No skills registered in project=%s, location=%s.",
            GCP_PROJECT_ID,
            SKILL_REGISTRY_LOCATION,
        )
        return 0
    log.info("%d skill(s) registered:", len(skills))
    for skill in skills:
        # Attribute access, not getattr-with-a-default: see search_skills.
        print(f"  {skill.name} — {skill.display_name or ''}")
    return 0


def _run_search(client: "Client | None", query: str) -> int:
    try:
        hits = search_skills(query, client=client)
    except Exception as exc:
        return 0 if _log_skip_or_failure("search skills", exc) else 1
    # No hits is a legitimate answer (and a useful negative result about
    # discoverability), not an error — exit 0 and say so.
    log.info("%d match(es) for %r:", len(hits), query)
    for hit in hits:
        print(f"  {hit.skill_name} — {hit.description or ''}")
    return 0


def _run_delete(client: "Client | None", skill_id: str, *, dry_run: bool) -> int:
    # No try/except: delete_skill classifies every error it can raise itself, and
    # the only thing that escapes it — SkillRegistryUnavailable from _skills_api —
    # is what main's outer handler exists for. _run_publish relies on the same.
    return (
        1 if delete_skill(skill_id, client=client, dry_run=dry_run).action == ACTION_FAILED else 0
    )


def main(argv: Sequence[str] | None = None) -> int:
    # First line only: the module docstring is a design note, not `--help` text.
    parser = argparse.ArgumentParser(description=(__doc__ or "").partition("\n")[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--publish",
        action="store_true",
        help="Create-or-update every skill in src.skills.definitions (default).",
    )
    group.add_argument("--list", action="store_true", help="List registered skills.")
    group.add_argument(
        "--search",
        metavar="QUERY",
        help="Semantic search the registry — the same retrieval an agent does at runtime.",
    )
    group.add_argument("--delete", metavar="SKILL_ID", help="Delete one skill by id.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only — make no registry calls (--publish / --delete; the read-only "
        "subcommands ignore it).",
    )
    args = parser.parse_args(argv)

    read_only = bool(args.list or args.search)
    client = None
    if read_only or not args.dry_run:
        # A --dry-run publish/delete constructs nothing: building the client runs
        # ADC discovery (and on a GCE/Cloud Run host, a metadata-server call), so
        # doing it anyway would make "makes no calls" quietly untrue.
        try:
            client = build_client()
        except Exception as exc:
            # The project/location are defaulted silently by build_client, so an
            # operator reading this line otherwise cannot tell what it addressed.
            what = (
                "construct the Agent Platform client for "
                f"project={GCP_PROJECT_ID}, location={SKILL_REGISTRY_LOCATION}"
            )
            return 0 if _log_skip_or_failure(what, exc) else 1

    try:
        if args.list:
            return _run_list(client)
        if args.search:
            return _run_search(client, args.search)
        if args.delete:
            return _run_delete(client, args.delete, dry_run=args.dry_run)
        return _run_publish(client, dry_run=args.dry_run)
    except SkillRegistryUnavailable as exc:
        log.info("%s — %s", _skip_message(), exc)
        return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
