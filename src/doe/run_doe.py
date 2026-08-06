"""DOE orchestrator CLI: design → launch → (optional) wait → harvest → analyze.

Safety: defaults to a DRY RUN (prints the design + cost estimate, submits
nothing). Real fan-out is opt-in via ``--execute`` because each engine_env
design point deploys a fresh Agent Engine — the heaviest/most expensive path.

Examples::

    # 1. See the plan + cost estimate, submit nothing (default)
    uv run --group doe python -m src.doe.run_doe --kind screening

    # 2. Cheap smoke: really submit 2 runs and wait for the report
    uv run --group doe python -m src.doe.run_doe --kind screening \
        --execute --max-runs 2 --wait

    # 3. Full screening (9 jobs), wait, harvest + analyze
    uv run --group doe python -m src.doe.run_doe --kind screening --execute --wait
"""

from __future__ import annotations

import argparse
import subprocess
from datetime import datetime

from src.doe.analyze import analyze
from src.doe.design import build_design
from src.doe.factors import get_factors, requires_fresh_deploy
from src.doe.harvest import harvest
from src.doe.launch import launch


def _default_experiment_id(kind: str, now: datetime | None = None) -> str:
    now = now or datetime.now()
    return f"doe-{kind}-{now.strftime('%Y%m%d-%H%M%S')}"


def run_experiment(
    *,
    kind: str = "screening",
    factor_names: list[str] | None = None,
    experiment_id: str | None = None,
    dry_run: bool = True,
    wait: bool = False,
    max_runs: int | None = None,
    reuse_agent_id: str = "",
    agent_module: str = "coordinator_agent",
    spec_dir: str = ".",
    out_dir: str | None = None,
    runner=subprocess.run,
) -> dict:
    """Design + (optionally) launch/harvest/analyze one experiment.

    Returns a summary dict with the experiment id, manifest, and (when run) the
    harvested DataFrame + report markdown.
    """
    factors = get_factors(factor_names)
    experiment_id = experiment_id or _default_experiment_id(kind)
    out_dir = out_dir or f"doe_runs/{experiment_id}"

    design = build_design(factors, kind=kind)
    if max_runs is not None and len(design) > max_runs:
        dropped = [p.design_point for p in design[max_runs:]]
        design = design[:max_runs]
        print(f"[max-runs {max_runs}] running {len(design)} points; dropped: {dropped}")

    fresh = sum(1 for _ in design) if requires_fresh_deploy(factors) else 0
    print(
        f"Experiment {experiment_id}: kind={kind}, {len(design)} design points, "
        f"factors={[f.name for f in factors]}"
    )
    print(
        f"Cost estimate: {fresh} fresh engine deploys (heaviest path), "
        f"{len(design) - fresh} reuse runs"
    )

    manifest = launch(
        design,
        factors,
        experiment_id,
        kind=kind,
        agent_module=agent_module,
        reuse_agent_id=reuse_agent_id,
        spec_dir=spec_dir,
        out_dir=out_dir,
        dry_run=dry_run,
        runner=runner,
    )

    summary = {"experiment_id": experiment_id, "manifest": manifest}

    if dry_run:
        print("[dry-run] nothing submitted. Re-run with --execute to launch.")
        return summary

    if wait:
        df = harvest(manifest, out_dir=out_dir, wait=True)
        summary["dataframe"] = df
        summary["report"] = analyze(df, factors, experiment_id, out_dir=out_dir)
        print(f"Report written to {out_dir}/report.md")
    else:
        print(
            f"Submitted {manifest['num_points']} jobs. Re-run with --wait "
            f"(or harvest manually) once they finish."
        )
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kind", choices=["screening", "full"], default="screening")
    parser.add_argument(
        "--factors",
        default="",
        help="Comma-separated factor names (default: all registered factors)",
    )
    parser.add_argument("--experiment-id", default="")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually submit jobs (default is a dry run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit dry run (default); prints the design and submits nothing",
    )
    parser.add_argument("--wait", action="store_true", help="Wait, then harvest + analyze")
    parser.add_argument("--max-runs", type=int, default=None, help="Cap the number of runs")
    parser.add_argument("--reuse-agent-id", default="", help="Reuse an engine (runner_env/param-only experiments)")
    parser.add_argument("--agent-module", default="coordinator_agent")
    parser.add_argument("--spec-dir", default=".")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args(argv)

    run_experiment(
        kind=args.kind,
        factor_names=[f.strip() for f in args.factors.split(",") if f.strip()] or None,
        experiment_id=args.experiment_id or None,
        dry_run=not args.execute,  # dry run unless --execute
        wait=args.wait,
        max_runs=args.max_runs,
        reuse_agent_id=args.reuse_agent_id,
        agent_module=args.agent_module,
        spec_dir=args.spec_dir,
        out_dir=args.out_dir or None,
    )


if __name__ == "__main__":
    main()
