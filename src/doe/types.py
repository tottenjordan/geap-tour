"""Declared shapes for the experiment-engine records.

The DOE manifests are the ones with money attached. A bake-off manifest is the
**only** record of the two Agent Engines a run deployed — `.env` holds one
coordinator id, not these — and teardown reads it back to delete them. A key that
quietly changes name here does not raise; it leaks engines that bill until someone
finds them by resource name in the console.

That is also why :func:`src.eval.artifacts.write_json_atomic` exists. Typing the
record and writing it atomically guard the same failure from two directions: one
stops the shape drifting, the other stops a Ctrl-C truncating it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NotRequired, TypedDict

if TYPE_CHECKING:
    from src.eval.types import PairwiseResult


class PointParams(TypedDict):
    """One design point's pipeline parameters, plus the bookkeeping to harvest it.

    ``gcs_results`` is where the point's ``full_results.json`` will land, computed
    **before** submission rather than discovered after — a point whose results
    cannot be located afterwards is a point that cost money and produced nothing.

    ``factor_env`` is separate from ``assignments`` on purpose: the assignments are
    the coded design (-1/+1), the env is what those levels mean as actual settings.
    Conflating them is how a design point's label stops matching what it ran.
    """

    # A stable string id ("dp01", "baseline") — NOT an index. Declared int on the
    # first pass here; the design generates labels, and "baseline" is one of them.
    design_point: str
    assignments: dict[str, Any]
    is_baseline: bool
    fresh_deploy: bool
    factor_env: dict[str, str]
    params: dict[str, Any]
    gcs_prefix: str
    gcs_results: str
    job_resource: str | None
    # Added after construction, per branch: `cmd` on a dry run (nothing submitted),
    # `returncode`/`error` once the subprocess has run. A dry-run entry and a failed
    # entry are different records and the harvest must be able to tell them apart.
    cmd: NotRequired[list[str]]
    returncode: NotRequired[int]
    error: NotRequired[str]


class DoeManifest(TypedDict):
    """The record of what a DOE launched — the only index into its results.

    ``fresh_deploys`` matters for cost reasoning: an ``engine_env`` factor deploys a
    new engine per point, which is the difference between a cheap sweep and an
    expensive one.
    """

    experiment_id: str
    kind: str
    factors: list[str]
    num_points: int
    points: list[PointParams]
    fresh_deploys: NotRequired[int]


class BakeoffPoint(TypedDict):
    """One backbone in a bake-off, and the engine it was deployed to.

    ``engine_id`` is the key with money attached — it is the ONLY record that this
    engine exists, and teardown reads it back to delete it. Declared required, and
    that is load-bearing: with ``points: list[dict]`` a dropped ``engine_id``
    type-checked clean, which a mutation test confirmed. An engine nobody can name
    is an engine nobody deletes.
    """

    design_point: str
    is_baseline: bool
    assignments: dict[str, Any]
    model_id: str
    engine_id: str | None


class BakeoffManifest(TypedDict):
    """The bake-off's own manifest. Same skeleton as :class:`DoeManifest`.

    Deliberately a separate type despite the overlap: ``run_bakeoff`` owns its
    deploy/teardown lifecycle directly rather than going through the DOE fan-out
    (whose per-point engines are deleted by the pipeline's exit handler before
    pairwise or traffic can reach them, and whose manifest records no ``engine_id``
    at all). The two manifests are written by different code for different readers,
    and the shared shape is a coincidence worth not encoding as inheritance.
    """

    experiment_id: str
    kind: str
    factors: list[str]
    num_points: int
    points: list[BakeoffPoint]


class BakeoffResult(TypedDict):
    """Everything a bake-off produced, including what it left running.

    ``kept_engines`` is the safety-relevant key: false means the ``finally`` tore
    both engines down, true means they are still billing. A reader who cannot tell
    which cannot know whether to go clean up.

    ``cost`` is ``n/a``-able rather than defaulting to zero — no measured
    ``usage_metadata`` means the price is unknown, and a fake $0 would make the
    cheaper backbone look free.

    The four always-present keys are the run's identity. The rest split by branch:
    a **dry run** adds ``steps`` (the plan) and stops there, because it deploys
    nothing — no engines, no cost, no pairwise, no report. An executed run reports
    what it DID instead. Everything is ``NotRequired`` rather than defaulted so the
    two cannot be confused: a dry-run result must not read as an executed one that
    happened to find nothing.
    """

    dry_run: bool
    baseline_model: str
    candidate_model: str
    out_dir: str
    # Dry-run only: the plan it would have executed. An executed run reports what it
    # DID (report/report_path), not what it intended to do.
    steps: NotRequired[list[str]]
    baseline_engine: NotRequired[str | None]
    candidate_engine: NotRequired[str | None]
    kept_engines: NotRequired[bool]
    quality: NotRequired[dict]
    cost: NotRequired[dict]
    pairwise: NotRequired[PairwiseResult | dict]
    report: NotRequired[str]
    report_path: NotRequired[str]
    experiment_name: NotRequired[str | None]


class DoeRunResult(TypedDict):
    """What ``run_doe`` hands back: the id to harvest under, and the manifest."""

    experiment_id: str
    manifest: DoeManifest
