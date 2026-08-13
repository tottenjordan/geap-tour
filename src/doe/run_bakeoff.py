"""One-command coordinator bake-off: Gemini vs Claude, end-to-end.

Chains the phases into a single entrypoint:

1. **Deploy** — two *persistent* coordinator engines that differ only by backbone
   (``gemini-3.6-flash`` baseline, ``claude-sonnet-5`` candidate). Each is deployed
   in its own interpreter (``src.doe.deploy_coordinator`` subprocess) so
   ``COORDINATOR_MODEL`` bakes at import time. The engines are recorded in the run
   manifest and torn down at the end unless ``--keep-engines``.
2. **Offline rubrics** — score each *deployed* engine with the batch eval (Vertex
   Gen AI Evaluation Service), per-model.
3. **Cost** — measure each engine's real per-request token usage over the eval
   prompts (``usage_metadata`` off ``stream_query``) and price it via
   :mod:`src.eval.cost_model`. If no usage surfaces, cost renders ``n/a`` rather
   than a misleading ``$0``.
4. **Pairwise SxS** — flip-debiased win-rate between the two engines.
5. **Labeled traffic** — synthetic load against each engine, tagged ``model=<id>``
   so Cloud Monitoring keeps them as separate series.
6. **Grouped verify** — ``verify_monitors --group-by model`` reads the two online
   series back, fused with offline + pairwise + cost into ``bakeoff_report.md``.

Safety: **dry-run is the default** (prints the plan, deploys nothing, spends
nothing). The live path is opt-in via ``--execute`` because it deploys two Agent
Engines (the heaviest, most expensive path).

Why not the DOE pipeline? The per-point KFP pipeline deploys an *ephemeral* engine
and deletes it in its exit handler, so the engines are gone before pairwise/traffic
can reach them — and it never records an engine id. The bake-off therefore owns the
deploy/teardown lifecycle directly.

Examples::

    # See the plan; deploy/spend nothing (default)
    uv run --group doe python -m src.doe.run_bakeoff

    # Full live bake-off: two deploys, offline + cost + pairwise + traffic, report
    uv run --group doe python -m src.doe.run_bakeoff --execute
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from src.doe.bakeoff_report import (
    build_bakeoff_report,
    online_from_grouped_monitors,
)
from src.doe.factors import get_factors

# The DOE main-effect direction fixes the roles: gemini (coded -1) = baseline,
# claude (coded +1) = candidate, so "candidate wins" reads as "Claude beats Gemini".
_BASELINE_LEVEL = "gemini"
_CANDIDATE_LEVEL = "claude"


def bakeoff_model_ids() -> tuple[str, str]:
    """Return ``(baseline_model_id, candidate_model_id)`` from the factor registry.

    Reads the ``model_backend`` factor so the two backbones are defined in exactly
    one place (the factor) rather than duplicated here.
    """
    factor = get_factors(["model_backend"])[0]
    baseline = factor.levels[_BASELINE_LEVEL]["COORDINATOR_MODEL"]
    candidate = factor.levels[_CANDIDATE_LEVEL]["COORDINATOR_MODEL"]
    return baseline, candidate


def _slug(model_id: str) -> str:
    """A display-name-safe slug for a model id (``claude-sonnet-5`` -> that)."""
    return "".join(c if c.isalnum() else "_" for c in model_id)


# --------------------------------------------------------------------------- #
# Default phase implementations (each injectable; stubbed in tests).
# --------------------------------------------------------------------------- #
def _deploy_engine(
    model_id: str, *, experiment_id: str | None = None, runner=subprocess.run
) -> str:
    """Deploy one persistent coordinator engine on ``model_id``; return its resource.

    Runs :mod:`src.doe.deploy_coordinator` in a fresh interpreter with
    ``COORDINATOR_MODEL`` set, because that variable is read once at import time and
    baked into the engine's env_vars — two backbones need two processes.
    """
    from src.doe.deploy_coordinator import parse_resource_from_output

    display = f"coordinator_agent_bakeoff_{_slug(model_id)}"
    result = runner(
        [sys.executable, "-m", "src.doe.deploy_coordinator", "--display-name", display],
        env={**os.environ, "COORDINATOR_MODEL": model_id},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"deploy of {model_id} failed (rc={result.returncode}):\n"
            f"{(result.stderr or '')[-2000:]}"
        )
    resource = parse_resource_from_output(result.stdout or "")
    if not resource:
        raise RuntimeError(
            f"deploy of {model_id} printed no engine resource:\n{(result.stdout or '')[-2000:]}"
        )
    print(f"Deployed {model_id} → {resource}")
    return resource


def _quality_from_batch(agent_result: dict) -> dict[str, float]:
    """Per-rubric 0-1 means from a ``multi_agent_batch_eval`` agent result.

    The batch eval keys metrics like ``agent_engine_0/tool_use_quality_v1``; map
    them to the canonical version-stripped base names so both engines share keys
    (delta math) and the report reads cleanly. Non-rubric metrics are ignored.
    """
    from src.doe.harvest import BATCH_METRICS, _metric_base

    out: dict[str, float] = {}
    for key, detail in (agent_result.get("metrics") or {}).items():
        base = _metric_base(key)
        if base in BATCH_METRICS and isinstance(detail, dict) and detail.get("score") is not None:
            out[base] = float(detail["score"])
    return out


def _score_engine(
    engine_id: str, model_id: str, *, out_dir: str, batch_fn=None
) -> dict[str, float]:
    """Offline-score one deployed engine's coordinator; return per-rubric 0-1 means."""
    if batch_fn is None:
        from src.eval.multi_agent_batch_eval import run_multi_agent_batch_eval as batch_fn
    batch = batch_fn(
        agents=["coordinator_agent"],
        agent_id=engine_id,
        output_path=os.path.join(out_dir, f"batch_{_slug(model_id)}.json"),
    )
    agent_result = (batch.get("agents") or {}).get("coordinator_agent") or {}
    return _quality_from_batch(agent_result)


def collect_token_usage(engine, prompts, *, user_id: str = "bakeoff-cost-probe") -> list[dict]:
    """Real per-request token usage for ``prompts`` off an engine's ``stream_query``.

    Reads ``usage_metadata`` from each streamed event: ``prompt_token_count`` is the
    running prompt size (take the max seen), ``candidates_token_count`` accrues across
    tool-call / thinking / answer events (sum). Returns one
    ``{"input_tokens", "output_tokens"}`` dict per prompt — the shape
    :func:`src.eval.cost_model.cost_summary` consumes. Missing usage counts as 0.
    """
    usages: list[dict] = []
    for prompt in prompts:
        in_tok = 0
        out_tok = 0
        for event in engine.stream_query(user_id=user_id, message=prompt):
            um = (event or {}).get("usage_metadata") or {}
            in_tok = max(in_tok, int(um.get("prompt_token_count", 0) or 0))
            out_tok += int(um.get("candidates_token_count", 0) or 0)
        usages.append({"input_tokens": in_tok, "output_tokens": out_tok})
    return usages


def _measure_usage(engine_id: str, model_id: str, *, client=None, cases=None) -> list[dict]:
    """Measure one engine's real token usage over the coordinator eval prompts."""
    if cases is None:
        from src.eval.agent_eval_configs import get_eval_cases

        cases = get_eval_cases("coordinator_agent")
    if client is None:
        from vertexai import Client

        from src.config import GCP_PROJECT_ID, GCP_REGION

        client = Client(project=GCP_PROJECT_ID, location=GCP_REGION)
    engine = client.agent_engines.get(name=engine_id)
    return collect_token_usage(engine, [c["prompt"] for c in cases])


def _cost_from_usages(model_id: str, usages: list[dict]) -> float | None:
    """Mean USD/request for ``model_id`` from measured usage; ``None`` if unmeasured.

    Returns ``None`` (not ``0.0``) when no tokens were captured, so the report shows
    an honest ``n/a`` instead of a fake ``$0``.
    """
    from src.eval.cost_model import cost_summary

    total = sum(
        int(u.get("input_tokens", 0) or 0) + int(u.get("output_tokens", 0) or 0) for u in usages
    )
    if total <= 0:
        return None
    return cost_summary(model_id, usages)["mean_usd_per_request"]


def _experiment_run_name(model_id: str) -> str:
    """A Vertex-Experiments-safe run name for a backbone.

    Vertex run names must be lowercase ``[a-z0-9-]`` (no dots), so
    ``gemini-3.6-flash`` becomes ``gemini-3-6-flash``.
    """
    safe = "".join(c if c.isalnum() else "-" for c in model_id.lower())
    return safe.strip("-") or "run"


def _experiment_metrics(
    model_id: str,
    quality: dict[str, float],
    pairwise: dict,
    online: dict[str, float],
    cost: float | None,
    *,
    is_baseline: bool,
) -> dict[str, float]:
    """Flatten one backbone's evidence streams into scalar experiment metrics.

    Rubric means + the backbone's *own* pairwise win rate + online latency/error +
    measured per-request cost. ``None`` cells are omitted (the experiments helper
    drops non-numeric values anyway).
    """
    metrics: dict[str, float] = dict(quality)
    wr = pairwise.get("win_rate_baseline") if is_baseline else pairwise.get("win_rate_candidate")
    if wr is not None:
        metrics["pairwise_win_rate"] = float(wr)
    for key in ("p50_latency", "p95_latency", "error_rate"):
        if online.get(key) is not None:
            metrics[key] = float(online[key])
    if cost is not None:
        metrics["cost_per_request"] = float(cost)
    return metrics


def _teardown_engine(engine_id: str) -> None:
    """Delete a deployed bake-off engine (best-effort; force to skip confirmation)."""
    from vertexai import agent_engines

    agent_engines.delete(engine_id, force=True)
    print(f"Deleted engine {engine_id}")


def _default_traffic_runner(
    engine_id: str,
    model: str,
    *,
    qps: int,
    duration_min: int,
    runner=subprocess.run,
) -> None:
    """Drive labeled synthetic load at one engine via the traffic CLI."""
    runner(
        [
            sys.executable,
            "-m",
            "src.traffic.generate_traffic",
            engine_id,
            "--load",
            "--emit-metrics",
            "--label",
            f"model={model}",
            "--qps",
            str(qps),
            "--duration",
            str(duration_min),
        ],
        check=True,
    )


def _write_manifest(
    out_dir: str,
    experiment_id: str | None,
    baseline_model: str,
    candidate_model: str,
    baseline_engine: str,
    candidate_engine: str,
) -> dict:
    """Record both deployed engines in a manifest (also usable by pairwise --from-manifest)."""
    manifest = {
        "experiment_id": experiment_id or "bakeoff",
        "kind": "bakeoff",
        "factors": ["model_backend"],
        "num_points": 2,
        "points": [
            {
                "design_point": _BASELINE_LEVEL,
                "is_baseline": True,
                "assignments": {"model_backend": _BASELINE_LEVEL},
                "model_id": baseline_model,
                "engine_id": baseline_engine,
            },
            {
                "design_point": _CANDIDATE_LEVEL,
                "is_baseline": False,
                "assignments": {"model_backend": _CANDIDATE_LEVEL},
                "model_id": candidate_model,
                "engine_id": candidate_engine,
            },
        ],
    }
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    return manifest


def _plan_steps(baseline: str, candidate: str, out_dir: str) -> list[str]:
    return [
        f"1. Deploy: two persistent coordinator engines ({baseline} + {candidate}), "
        f"one subprocess each (COORDINATOR_MODEL baked at import) → {out_dir}/manifest.json",
        "2. Score: offline rubrics per deployed engine (Vertex Gen AI Evaluation Service)",
        "3. Cost: measure real per-request token usage per engine → fair token→$ / GSU",
        "4. Pairwise SxS: flip-debiased win-rate (gemini=baseline, claude=candidate)",
        f"5. Traffic: labeled synthetic load per engine (--label model={baseline} / {candidate})",
        "6. Verify + Report: verify_monitors --group-by model, fuse everything → "
        f"{out_dir}/bakeoff_report.md",
        "7. Teardown: delete both engines (skip with --keep-engines)",
    ]


def run_bakeoff(
    *,
    experiment_id: str | None = None,
    dry_run: bool = True,
    out_dir: str | None = None,
    traffic_qps: int = 2,
    traffic_duration_min: int = 1,
    monitor_hours: int = 1,
    keep_engines: bool = False,
    skip_preflight: bool = False,
    experiment_name: str | None = None,
    # Injectable phase entrypoints (default to the real ones; stubbed in tests).
    preflight_fn=None,
    deploy_fn=None,
    score_fn=None,
    usage_fn=None,
    pairwise_fn=None,
    verify_fn=None,
    traffic_runner=None,
    teardown_fn=None,
    log_run_fn=None,
) -> dict:
    """Run (or plan) the full Gemini-vs-Claude coordinator bake-off.

    With ``dry_run=True`` (default) nothing is deployed or spent: returns a plan
    dict. With ``dry_run=False`` it deploys two engines, scores them, and writes the
    report — always tearing the engines down in a ``finally`` unless ``keep_engines``.
    """
    baseline_model, candidate_model = bakeoff_model_ids()
    out_dir = out_dir or (f"doe_runs/{experiment_id}" if experiment_id else "doe_runs/bakeoff")
    steps = _plan_steps(baseline_model, candidate_model, out_dir)

    if dry_run:
        print(f"[dry-run] coordinator bake-off: {baseline_model} vs {candidate_model}")
        for s in steps:
            print(f"  {s}")
        print("[dry-run] nothing deployed. Re-run with --execute to launch.")
        return {
            "dry_run": True,
            "baseline_model": baseline_model,
            "candidate_model": candidate_model,
            "out_dir": out_dir,
            "steps": steps,
        }

    # Preflight: confirm both backbones are actually served BEFORE the two
    # (expensive) deploys. Opt out with skip_preflight.
    if not skip_preflight:
        if preflight_fn is None:
            from src.eval.preflight import ensure_models_served as preflight_fn
        print(f"Preflight: checking [{baseline_model}, {candidate_model}] are served…")
        preflight_fn([baseline_model, candidate_model])
        print("Preflight: both backbones served ✓")

    # Bind real entrypoints lazily so a dry run / import needs no heavy deps.
    deploy_fn = deploy_fn or _deploy_engine
    score_fn = score_fn or _score_engine
    usage_fn = usage_fn or _measure_usage
    if pairwise_fn is None:
        from src.eval.pairwise_eval import run_pairwise_eval as pairwise_fn
    if verify_fn is None:
        from src.eval.verify_monitors import verify_monitor_results as verify_fn
    traffic_runner = traffic_runner or _default_traffic_runner
    teardown_fn = teardown_fn or _teardown_engine
    if log_run_fn is None:
        from src.observability.experiments import log_run as log_run_fn

    engines: dict[str, str] = {}  # model_id -> engine resource name
    try:
        # 1. Deploy two persistent engines (one subprocess per backbone).
        for model_id in (baseline_model, candidate_model):
            engines[model_id] = deploy_fn(model_id, experiment_id=experiment_id)
        baseline_engine = engines[baseline_model]
        candidate_engine = engines[candidate_model]
        _write_manifest(
            out_dir,
            experiment_id,
            baseline_model,
            candidate_model,
            baseline_engine,
            candidate_engine,
        )

        # 2-3. Offline rubrics + real per-request cost, per deployed engine.
        quality: dict[str, dict[str, float]] = {}
        cost: dict[str, float] = {}
        for model_id, engine_id in (
            (baseline_model, baseline_engine),
            (candidate_model, candidate_engine),
        ):
            quality[model_id] = score_fn(engine_id, model_id, out_dir=out_dir)
            unit_cost = _cost_from_usages(model_id, usage_fn(engine_id, model_id))
            if unit_cost is not None:
                cost[model_id] = unit_cost

        # 4. Pairwise SxS.
        pairwise = pairwise_fn(baseline_engine, candidate_engine)

        # 5. Labeled synthetic traffic at each engine.
        for engine_id, model in (
            (baseline_engine, baseline_model),
            (candidate_engine, candidate_model),
        ):
            traffic_runner(engine_id, model, qps=traffic_qps, duration_min=traffic_duration_min)

        # 6. Read the two online series back (split by model label) and fuse.
        grouped = verify_fn(output_format="json", hours=monitor_hours, group_by="model")
        online = online_from_grouped_monitors(grouped or {})
        report = build_bakeoff_report(
            quality,
            pairwise or {},
            online,
            cost,
            baseline=baseline_model,
            candidate=candidate_model,
            experiment_id=experiment_id,
        )

        os.makedirs(out_dir, exist_ok=True)
        report_path = os.path.join(out_dir, "bakeoff_report.md")
        with open(report_path, "w") as f:
            f.write(report)
        print(f"Bake-off report written to {report_path}")

        # 7. Durable comparison record: one Vertex Experiments run per backbone, in
        # the coordinator's own experiment (never mixed with the router's series).
        # Best-effort — a completed report must never be undone by a side-record, and
        # the helper no-ops entirely when experiment_name is unset (dormant default).
        try:
            for model_id, is_baseline in ((baseline_model, True), (candidate_model, False)):
                log_run_fn(
                    experiment=experiment_name,
                    run=_experiment_run_name(model_id),
                    params={
                        "backbone": model_id,
                        "role": "baseline" if is_baseline else "candidate",
                    },
                    metrics=_experiment_metrics(
                        model_id,
                        quality.get(model_id, {}),
                        pairwise or {},
                        online.get(model_id, {}),
                        cost.get(model_id),
                        is_baseline=is_baseline,
                    ),
                )
        except Exception as e:  # pylint: disable=broad-exception-caught
            print(f"WARNING: failed to log Vertex Experiments runs: {e}")

        return {
            "dry_run": False,
            "baseline_model": baseline_model,
            "candidate_model": candidate_model,
            "baseline_engine": baseline_engine,
            "candidate_engine": candidate_engine,
            "out_dir": out_dir,
            "quality": quality,
            "cost": cost,
            "pairwise": pairwise,
            "experiment_name": experiment_name,
            "report": report,
            "report_path": report_path,
            "kept_engines": keep_engines,
        }
    finally:
        # Guaranteed teardown: deployed engines cost money whether or not the run
        # completed. --keep-engines opts out (e.g. to re-run pairwise by hand).
        if not keep_engines:
            for model_id, engine_id in engines.items():
                try:
                    teardown_fn(engine_id)
                except Exception as e:  # pylint: disable=broad-exception-caught
                    print(f"WARNING: failed to delete {model_id} engine {engine_id}: {e}")
        elif engines:
            print(
                f"--keep-engines: leaving {len(engines)} engine(s) running: {list(engines.values())}"
            )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--experiment-id", default="")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually deploy + run (default is a dry run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit dry run (default); prints the plan and does nothing",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="(no-op, kept for compatibility; the persistent-deploy path is synchronous)",
    )
    parser.add_argument("--out-dir", default="")
    parser.add_argument(
        "--keep-engines",
        action="store_true",
        help="Do NOT delete the two deployed engines at the end (default: tear down)",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip the model-availability check before deploying",
    )
    parser.add_argument(
        "--experiment-name",
        default="coordinator-bakeoff",
        help=(
            "Vertex Experiments name to record one run per backbone into "
            "(default: coordinator-bakeoff; pass '' to disable). Kept separate from "
            "the router's 'router-efficiency' experiment."
        ),
    )
    parser.add_argument("--traffic-qps", type=int, default=2)
    parser.add_argument("--traffic-duration-min", type=int, default=1)
    parser.add_argument("--monitor-hours", type=int, default=1)
    args = parser.parse_args(argv)

    run_bakeoff(
        experiment_id=args.experiment_id or None,
        dry_run=not args.execute,
        out_dir=args.out_dir or None,
        keep_engines=args.keep_engines,
        skip_preflight=args.skip_preflight,
        experiment_name=args.experiment_name or None,
        traffic_qps=args.traffic_qps,
        traffic_duration_min=args.traffic_duration_min,
        monitor_hours=args.monitor_hours,
    )


if __name__ == "__main__":
    main()
