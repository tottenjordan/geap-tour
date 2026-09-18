"""Declared shapes for the deploy-side records.

These describe what a **deployed engine actually is**, as opposed to what the code
that deployed it intended. That gap is the premise of this whole package: "a merged
fix is not a deployed fix". Serving config drifts silently — the engine keeps
returning 200, health checks pass, nothing logs — so the records that carry a live
spec back for comparison are exactly the ones where a quietly-renamed or
quietly-dropped key would restore the silence.

Kept separate from :mod:`src.eval.types` because they answer a different question
and have no consumer in common.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict

if TYPE_CHECKING:
    # TYPE_CHECKING only: engine_baseline imports FindingDict from here at runtime,
    # so a real import would be circular. `Finding` is the dataclass; `FindingDict`
    # below is its serialized form, and check_engine carries the dataclasses.
    from src.deploy.engine_baseline import Finding


class FindingDict(TypedDict):
    """One baseline check against a live engine, serialized.

    ``severity`` is a ``Literal``, and it is the key the exit code is computed from:
    only ``critical`` fails a run, because advisories that fail CI get the whole
    check suppressed. ``"Critical"`` or ``"CRITICAL"`` would be a perfectly valid
    ``str`` that silently never matches — turning every finding advisory and the
    verifier into a no-op that reports clean.

    ``why`` is required, not decorative. A finding a reader cannot act on gets
    ignored, and these fire days after the change that caused them.
    """

    name: str
    ok: bool
    severity: Literal["critical", "advisory"]
    expected: Any
    observed: Any
    why: str


class EngineSpec(TypedDict):
    """A live engine's serving config, normalized for comparison.

    Every key here has been a silent failure at least once: a 4Gi container
    OOM-killing workers into empty-at-200, ``min_instances`` dropping to 1 and
    recycling containers mid-invocation, router tier env regressed to Gemini-3 by a
    plain ``--update``, an engine running as the default SA rather than its own
    ``AGENT_IDENTITY``.

    ``effective_identity`` is what the engine's own outbound calls authenticate as —
    the principal that needs ``agentregistry.viewer`` and ``modelarmor.user``. Not
    the same thing as the deploying user, which is the confusion that made an IAM
    denial look like an unreachable API.

    Most fields are Optional because ``normalize`` reads them with ``.get()`` and
    **"unset" is a distinct, meaningful state** — its docstring says so explicitly.
    A missing ``resource_limits`` means the engine took the 4Gi default, which is
    the single most common cause of empty-at-200 here; collapsing that to ``{}``
    would make "never configured" indistinguishable from "configured to nothing".
    """

    engine_id: str
    display_name: str | None
    env: dict[str, str]
    resource_limits: dict[str, str] | None
    min_instances: int | None
    labels: dict[str, str]
    identity_type: str | None
    effective_identity: str | None
    agent_gateway_config: Any
    update_time: str | None
    # Synthetic: not part of the engine resource, injected by `check_engine` so one
    # project-policy read serves every engine. The leading underscore marks it as
    # not-from-the-API, and declaring it here is what keeps that convention from
    # looking like a typo to the next reader.
    _modelarmor_grantees: NotRequired[Any]


class EngineCheckResult(TypedDict):
    """The verdict for one engine: its findings, and whether any were critical.

    ``ok`` is **not** ``all(f["ok"] for f in findings)`` — it is "no *critical*
    finding failed". Advisories are reported and do not fail. An empty ``findings``
    list with ``ok=True`` would be a vacuous pass, which is why ``error`` exists as
    a separate key: a spec that could not be fetched is not an engine that passed.
    """

    engine_id: str
    ok: bool
    # The dataclasses, not their serialized form — `as_dict()` is applied later, at
    # the JSON boundary. Two shapes for one concept, kept distinct so a reader knows
    # which one they have.
    findings: list[Finding]
    # Absent on the fetch-error branch, which returns before `role` is recorded.
    # Left NotRequired rather than backfilled — this is a typing change.
    role: NotRequired[str]
    display_name: NotRequired[str | None]
    updated: NotRequired[str]
    error: NotRequired[str]


class OrphanScan(TypedDict):
    """A census of reasoning engines in the project, bucketed by what owns them.

    ``orphans`` are ours and unaccounted for — the leak a killed bake-off produces,
    which bills until somebody finds it by resource name in the console.
    ``unlabelled`` is deliberately its own bucket rather than folded into orphans:
    an engine with no labels may belong to someone else on a shared project, and
    deleting it is not our call.

    ``total_engines`` and ``ours`` are counts; everything else is a LIST of engine
    records. ``kept`` in particular was declared an int on the first pass here —
    it is the set of engines deliberately retained (the demo probe among them), and
    a reader needs to see which, not how many.
    """

    total_engines: int
    ours: int
    kept: list[dict]
    orphans: list[dict]
    dangling: list[dict]
    unlabelled: list[dict]


class AgentDeployConfig(TypedDict):
    """The kwargs handed to ``agent_engines.create``/``update``.

    ``requirements`` is the serving dependency set, which must agree with
    ``pyproject.toml`` exactly: the AdkApp is cloudpickled locally and unpickled by
    whatever the container installed, so "compatible" is not the bar.
    ``extra_packages`` carries ``src`` into the runtime — without it the deployed
    agent cannot import its own config.

    The deployment settings are ``NotRequired`` because ``_build_config`` adds them
    conditionally, and the distinction is load-bearing on **update**: an absent
    ``min_instances`` means "leave whatever is running alone", which is why a
    routine ``--update`` never downgrades an engine already at 4 replicas. Writing a
    default here instead of omitting the key would silently do exactly that.
    """

    display_name: str
    requirements: list[str]
    extra_packages: list[str]
    env_vars: dict[str, str]
    labels: dict[str, str]
    staging_bucket: str
    min_instances: NotRequired[int | None]
    resource_limits: NotRequired[dict[str, str]]
    identity_type: NotRequired[str]
    agent_gateway_config: NotRequired[Any]
