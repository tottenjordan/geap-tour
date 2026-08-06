"""Fan out one Vertex ``PipelineJob`` per design point.

Each design point runs as its *own* ``src.pipelines.submit`` subprocess. That is
deliberate (see docs/plans): the eval pipeline bakes factor env vars at compile
time and the deployed engine reads config at import time, so a fresh interpreter
with that point's env is the only way to get a config-overridden variant. The
subprocess prints its ``PipelineJob`` resource name on the last stdout line,
which we capture into a manifest for the harvester.

env-channel factors (engine_env / runner_env) are injected into the subprocess
environment; param-channel factors (eval fidelity) become CLI flags.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from src.config import GCP_STAGING_BUCKET
from src.doe.design import DesignPoint
from src.doe.factors import Factor, requires_fresh_deploy

_JOB_PREFIX = "Submitted PipelineJob: "


def build_point_env(point: DesignPoint, factors: list[Factor]) -> dict[str, str]:
    """Merge env-channel (engine_env + runner_env) assignments for a point."""
    env: dict[str, str] = {}
    for f in factors:
        if f.channel in ("engine_env", "runner_env"):
            env.update(f.assignment(point.assignments[f.name]))
    return env


def build_point_params(point: DesignPoint, factors: list[Factor]) -> dict:
    """Merge param-channel assignments (native types) for a point."""
    params: dict = {}
    for f in factors:
        if f.channel == "param":
            params.update(f.assignment(point.assignments[f.name]))
    return params


def _params_to_cli(params: dict) -> list[str]:
    args: list[str] = []
    for name, value in params.items():
        args += [f"--{name.replace('_', '-')}", str(value)]
    return args


def _parse_job_resource(stdout: str) -> str | None:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith(_JOB_PREFIX):
            return line[len(_JOB_PREFIX):].strip()
    return None


def submit_point(
    point: DesignPoint,
    factors: list[Factor],
    experiment_id: str,
    *,
    agent_module: str = "coordinator_agent",
    reuse_agent_id: str = "",
    spec_dir: str = ".",
    dry_run: bool = False,
    runner=subprocess.run,
) -> dict:
    """Submit one design point; return its manifest entry.

    When any active factor is engine_env (or no reuse engine is given) a fresh
    engine is deployed via ``--agent-module``; otherwise the run reuses
    ``reuse_agent_id`` (cheaper, for runner_env/param-only experiments).
    """
    fresh_deploy = requires_fresh_deploy(factors) or not reuse_agent_id
    factor_env = build_point_env(point, factors)
    params = build_point_params(point, factors)
    spec_path = os.path.join(spec_dir, f"eval_pipeline_{experiment_id}_{point.design_point}.yaml")
    gcs_prefix = f"eval-results/doe/{experiment_id}/{point.design_point}"

    cmd = [
        sys.executable, "-m", "src.pipelines.submit",
        "--experiment-id", experiment_id,
        "--design-point", point.design_point,
        "--spec-path", spec_path,
        *_params_to_cli(params),
    ]
    if fresh_deploy:
        cmd += ["--agent-module", agent_module]
    else:
        cmd += ["--agent-id", reuse_agent_id]

    entry = {
        "design_point": point.design_point,
        "is_baseline": point.is_baseline,
        "assignments": dict(point.assignments),
        "factor_env": factor_env,
        "params": params,
        "fresh_deploy": fresh_deploy,
        "gcs_prefix": gcs_prefix,
        "gcs_results": f"gs://{GCP_STAGING_BUCKET}/{gcs_prefix}/full_results.json",
        "job_resource": None,
    }

    if dry_run:
        entry["cmd"] = cmd
        return entry

    result = runner(
        cmd,
        env={**os.environ, **factor_env},
        capture_output=True,
        text=True,
        check=False,
    )
    entry["returncode"] = result.returncode
    if result.returncode != 0:
        entry["error"] = (result.stderr or "")[-2000:]
        return entry
    entry["job_resource"] = _parse_job_resource(result.stdout or "")
    return entry


def build_manifest(
    design: list[DesignPoint],
    factors: list[Factor],
    experiment_id: str,
    entries: list[dict],
    kind: str,
) -> dict:
    return {
        "experiment_id": experiment_id,
        "kind": kind,
        "factors": [f.name for f in factors],
        "num_points": len(design),
        "fresh_deploys": sum(1 for e in entries if e.get("fresh_deploy")),
        "points": entries,
    }


def write_manifest(manifest: dict, out_dir: str) -> str:
    """Write the manifest locally and (best-effort) to GCS. Returns local path."""
    os.makedirs(out_dir, exist_ok=True)
    local_path = os.path.join(out_dir, "manifest.json")
    with open(local_path, "w") as f:
        json.dump(manifest, f, indent=2)

    prefix = f"eval-results/doe/{manifest['experiment_id']}"
    try:
        from google.cloud import storage

        client = storage.Client()
        bucket = client.bucket(GCP_STAGING_BUCKET)
        bucket.blob(f"{prefix}/manifest.json").upload_from_filename(local_path)
        print(f"manifest → gs://{GCP_STAGING_BUCKET}/{prefix}/manifest.json")
    except Exception as e:
        print(f"manifest GCS upload skipped: {e}")
    return local_path


def launch(
    design: list[DesignPoint],
    factors: list[Factor],
    experiment_id: str,
    *,
    kind: str = "screening",
    agent_module: str = "coordinator_agent",
    reuse_agent_id: str = "",
    spec_dir: str = ".",
    out_dir: str | None = None,
    dry_run: bool = False,
    runner=subprocess.run,
) -> dict:
    """Submit every design point and write the run manifest."""
    out_dir = out_dir or os.path.join("doe_runs", experiment_id)
    entries = [
        submit_point(
            p, factors, experiment_id,
            agent_module=agent_module,
            reuse_agent_id=reuse_agent_id,
            spec_dir=spec_dir,
            dry_run=dry_run,
            runner=runner,
        )
        for p in design
    ]
    manifest = build_manifest(design, factors, experiment_id, entries, kind)
    fresh = manifest["fresh_deploys"]
    print(
        f"{'[dry-run] ' if dry_run else ''}experiment {experiment_id}: "
        f"{len(design)} runs, {fresh} fresh-deploy (heaviest path), "
        f"{len(design) - fresh} reuse"
    )
    write_manifest(manifest, out_dir)
    return manifest
