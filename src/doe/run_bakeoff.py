"""One-command coordinator bake-off: Gemini vs Claude, end-to-end.

Chains the five phases into a single entrypoint:

1. **DOE fan-out** — a single-factor (``model_backend``) ``full`` design deploys
   two fresh coordinator engines (``gemini-3.6-flash`` baseline, ``claude-sonnet-5``
   candidate), runs the per-engine offline pipeline, and harvests ``results.csv``.
2. **Pairwise SxS** — flip-debiased win-rate between the two engines.
3. **Labeled traffic** — synthetic load against each engine, tagged
   ``model=<id>`` so Cloud Monitoring keeps them as separate series.
4. **Grouped verify** — ``verify_monitors --group-by model`` reads the two online
   series back.
5. **Bake-off report** — fuses offline rubrics + win-rate + online latency/error +
   cost into ``bakeoff_report.md`` with a one-line verdict.

Safety: **dry-run is the default** (prints the plan, deploys nothing, spends
nothing) — mirrors ``run_doe``. The live path is opt-in via ``--execute`` because
step 1 deploys two Agent Engines (the heaviest, most expensive path).

Examples::

    # See the plan; deploy/submit/spend nothing (default)
    uv run --group doe python -m src.doe.run_bakeoff

    # Full live bake-off: two deploys, offline + pairwise + traffic, then report
    uv run --group doe python -m src.doe.run_bakeoff --execute --wait
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
    quality_from_results_frame,
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


def _level_to_model() -> dict[str, str]:
    baseline, candidate = bakeoff_model_ids()
    return {_BASELINE_LEVEL: baseline, _CANDIDATE_LEVEL: candidate}


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


def _engines_from_summary(summary: dict, out_dir: str) -> tuple[str, str]:
    """Pull ``(baseline_engine, candidate_engine)`` from the DOE summary/manifest.

    Prefers the inline manifest in the summary; falls back to
    ``<out_dir>/manifest.json`` on disk. Delegates the gemini/claude split to the
    pairwise helper so the convention lives in one place.
    """
    from src.eval.pairwise_eval import load_engines_from_manifest

    manifest = summary.get("manifest")
    if manifest is None:
        with open(os.path.join(out_dir, "manifest.json")) as f:
            manifest = json.load(f)
    return load_engines_from_manifest(manifest)


def _plan_steps(baseline: str, candidate: str, out_dir: str) -> list[str]:
    return [
        f"1. DOE: full single-factor design (model_backend) → deploy {baseline} + "
        f"{candidate}, run offline pipeline, harvest → {out_dir}/results.csv",
        "2. Pairwise SxS: flip-debiased win-rate (gemini=baseline, claude=candidate)",
        f"3. Traffic: labeled synthetic load per engine (--label model={baseline} / {candidate})",
        "4. Verify: verify_monitors --group-by model (two online series)",
        f"5. Report: fuse offline + pairwise + online + cost → {out_dir}/bakeoff_report.md",
    ]


def run_bakeoff(
    *,
    experiment_id: str | None = None,
    dry_run: bool = True,
    wait: bool = True,
    out_dir: str | None = None,
    traffic_qps: int = 2,
    traffic_duration_min: int = 1,
    monitor_hours: int = 1,
    cost: dict[str, float] | None = None,
    # Injectable phase entrypoints (default to the real ones; stubbed in tests).
    doe_fn=None,
    pairwise_fn=None,
    verify_fn=None,
    traffic_runner=None,
) -> dict:
    """Run (or plan) the full Gemini-vs-Claude coordinator bake-off.

    With ``dry_run=True`` (default) nothing is deployed or spent: returns a plan
    dict. With ``dry_run=False`` it executes every phase and writes the report.
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

    # Bind real entrypoints lazily so a dry run / import needs no heavy deps.
    if doe_fn is None:
        from src.doe.run_doe import run_experiment as doe_fn
    if pairwise_fn is None:
        from src.eval.pairwise_eval import run_pairwise_eval as pairwise_fn
    if verify_fn is None:
        from src.eval.verify_monitors import verify_monitor_results as verify_fn
    if traffic_runner is None:
        traffic_runner = _default_traffic_runner

    # 1. DOE fan-out (two fresh deploys) → harvested dataframe + manifest.
    summary = doe_fn(
        kind="full",
        factor_names=["model_backend"],
        experiment_id=experiment_id,
        dry_run=False,
        wait=wait,
        out_dir=out_dir,
    )
    df = summary.get("dataframe")

    # 2. Read the two engine ids and run the pairwise SxS.
    baseline_engine, candidate_engine = _engines_from_summary(summary, out_dir)
    pairwise = pairwise_fn(baseline_engine, candidate_engine)

    # 3. Labeled synthetic traffic at each engine.
    for engine_id, model in (
        (baseline_engine, baseline_model),
        (candidate_engine, candidate_model),
    ):
        traffic_runner(engine_id, model, qps=traffic_qps, duration_min=traffic_duration_min)

    # 4. Read the two online series back, split by model label.
    grouped = verify_fn(output_format="json", hours=monitor_hours, group_by="model")

    # 5. Fuse everything into the report.
    quality = quality_from_results_frame(df, _level_to_model()) if df is not None else {}
    online = online_from_grouped_monitors(grouped or {})
    report = build_bakeoff_report(
        quality,
        pairwise or {},
        online,
        cost or {},
        baseline=baseline_model,
        candidate=candidate_model,
        experiment_id=experiment_id,
    )

    os.makedirs(out_dir, exist_ok=True)
    report_path = os.path.join(out_dir, "bakeoff_report.md")
    with open(report_path, "w") as f:
        f.write(report)
    print(f"Bake-off report written to {report_path}")

    return {
        "dry_run": False,
        "baseline_model": baseline_model,
        "candidate_model": candidate_model,
        "baseline_engine": baseline_engine,
        "candidate_engine": candidate_engine,
        "out_dir": out_dir,
        "pairwise": pairwise,
        "report": report,
        "report_path": report_path,
    }


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
        help="Wait for the DOE pipeline to finish before harvesting",
    )
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--traffic-qps", type=int, default=2)
    parser.add_argument("--traffic-duration-min", type=int, default=1)
    parser.add_argument("--monitor-hours", type=int, default=1)
    args = parser.parse_args(argv)

    run_bakeoff(
        experiment_id=args.experiment_id or None,
        dry_run=not args.execute,
        wait=args.wait,
        out_dir=args.out_dir or None,
        traffic_qps=args.traffic_qps,
        traffic_duration_min=args.traffic_duration_min,
        monitor_hours=args.monitor_hours,
    )


if __name__ == "__main__":
    main()
