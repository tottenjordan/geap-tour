"""KFP v2 function-based components wrapping the existing GEAP eval functions.

IMPORTANT: every ``from src... import ...`` MUST live inside a component
function body, never at module top level. ``src.config`` reads deployment
env vars at import time, and those vars are injected per-task at runtime via
KFP ``.set_env_variable(...)``. Top-level ``src.*`` imports would crash the
container before the env is populated.
"""

from typing import NamedTuple

from kfp import dsl  # ty: ignore[unresolved-import]

IMAGE = "us-central1-docker.pkg.dev/hybrid-vertex/geap-eval/eval-runner:v3"


@dsl.component(base_image=IMAGE)
def resolve_agent(
    agent_id: str,
    agent_module: str,
    display_name: str = "",
) -> NamedTuple("Out", [("agent_resource", str), ("deployed_fresh", bool)]):  # ty: ignore[invalid-type-form]
    from src.config import GCP_PROJECT_ID, GCP_REGION

    if agent_id:
        if agent_id.startswith("projects/"):
            resource = agent_id
        else:
            resource = (
                f"projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/reasoningEngines/{agent_id}"
            )
        return (resource, False)

    import importlib

    from src.deploy.deploy_agents import deploy_agent

    # Deploy the temp engine under a unique display_name so the exit-handler
    # cleanup task can find and delete it using pipeline params alone.
    mod = importlib.import_module(f"src.agents.{agent_module}")
    res = deploy_agent(getattr(mod, agent_module), display_name=display_name or None)
    return (res, True)


@dsl.component(base_image=IMAGE)
def generate_traffic(agent_resource: str, count: int = 2):
    from src.traffic.generate_traffic import generate_traffic as _gen

    _gen(agent_resource_name=agent_resource, count=count)


@dsl.component(base_image=IMAGE)
def batch_eval(
    agent_resource: str,
    threshold: float,
    results: dsl.Output[dsl.Artifact],
    metrics: dsl.Output[dsl.Metrics],
) -> bool:
    from src.eval.multi_agent_batch_eval import run_multi_agent_batch_eval

    r = run_multi_agent_batch_eval(
        agent_id=agent_resource,
        score_threshold=threshold,
        output_path=results.path,
    )

    for agent, info in (r.get("agents") or {}).items():
        for m, mv in (info.get("metrics") or {}).items():
            score = (mv or {}).get("score")
            if score is not None:
                metrics.log_metric(f"{agent}.{m}", score)

    return bool(r.get("all_passed"))


@dsl.component(base_image=IMAGE)
def simulated_eval(
    agent_resource: str,
    threshold: float,
    results: dsl.Output[dsl.Artifact],
    scenario_count: int = 5,
    max_turns: int = 3,
) -> bool:
    import json

    from src.eval.simulated_eval import run_simulated_eval

    outcomes: dict = {}
    for name in ("coordinator_agent", "travel_agent"):
        try:
            passed = run_simulated_eval(
                agent_resource_name=agent_resource,
                agent_name=name,
                scenario_count=scenario_count,
                max_turns=max_turns,
                score_threshold=threshold,
            )
            outcomes[name] = {"passed": bool(passed)}
        except Exception as e:
            outcomes[name] = {"passed": False, "error": str(e)}

    with open(results.path, "w") as f:
        json.dump(outcomes, f, indent=2)

    return all(o.get("passed") for o in outcomes.values())


@dsl.component(base_image=IMAGE)
def complexity_eval(
    results: dsl.Output[dsl.Artifact],
    metrics: dsl.Output[dsl.Metrics],
):
    import asyncio
    import json

    from src.eval.agent_eval_configs import ROUTER_EVAL_CASES
    from src.eval.complexity_metrics import (
        run_complexity_accuracy_eval,
        run_cost_efficiency_eval,
    )

    accuracy = asyncio.run(run_complexity_accuracy_eval(ROUTER_EVAL_CASES))
    cost = asyncio.run(run_cost_efficiency_eval(ROUTER_EVAL_CASES))

    with open(results.path, "w") as f:
        json.dump({"accuracy": accuracy, "cost_efficiency": cost}, f, indent=2)

    try:
        val = float(str(accuracy.get("accuracy_pct")).rstrip("%"))
        metrics.log_metric("classifier_accuracy_pct", val)
    except Exception as e:
        print(f"skipped logging classifier_accuracy_pct: {e}")


@dsl.component(base_image=IMAGE)
def monitor_verify(agent_resource: str, results: dsl.Output[dsl.Artifact]):
    import json

    from src.eval.verify_monitors import verify_monitor_results

    _ = agent_resource  # accepted for DAG ordering
    data = verify_monitor_results(output_format="json")

    with open(results.path, "w") as f:
        json.dump(data or {}, f, indent=2)


@dsl.component(base_image=IMAGE)
def report(
    batch_results: dsl.Input[dsl.Artifact],
    sim_results: dsl.Input[dsl.Artifact],
    complexity_results: dsl.Input[dsl.Artifact],
    monitor_results: dsl.Input[dsl.Artifact],
    report_md: dsl.Output[dsl.Markdown],
    full_results: dsl.Output[dsl.Artifact],
    experiment_id: str = "",
    design_point: str = "",
):
    import json

    from src.config import GCP_STAGING_BUCKET
    from src.eval.run_all_evals import build_report

    def _load(path: str) -> dict:
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return {}

    batch = _load(batch_results.path)
    run_id = batch.get("run_id", "pipeline_run")
    results = {
        "run_id": run_id,
        "timestamp": batch.get("timestamp", ""),
        "agent": batch.get("agent_engine", ""),
        "threshold": batch.get("score_threshold", 3.0),
        "experiment_id": experiment_id,
        "design_point": design_point,
        "batch": batch,
        "simulated": _load(sim_results.path),
        "complexity": _load(complexity_results.path),
        "monitors": _load(monitor_results.path),
    }

    md = build_report(results)

    with open(report_md.path, "w") as f:
        f.write(md)
    with open(full_results.path, "w") as f:
        f.write(json.dumps(results, indent=2, default=str))

    try:
        from google.cloud import storage

        client = storage.Client()
        bucket = client.bucket(GCP_STAGING_BUCKET)
        # DOE runs land under a deterministic experiment/design-point prefix so
        # the harvester can find each design point's results; ad-hoc runs keep
        # the per-run_id layout.
        if experiment_id and design_point:
            prefix = f"eval-results/doe/{experiment_id}/{design_point}"
        else:
            prefix = f"eval-results/{run_id}"
        bucket.blob(f"{prefix}/report.md").upload_from_filename(report_md.path)
        bucket.blob(f"{prefix}/full_results.json").upload_from_filename(full_results.path)
        print(f"uploaded results to gs://{GCP_STAGING_BUCKET}/{prefix}/")
    except Exception as e:
        print(f"result upload skipped: {e}")


@dsl.component(base_image=IMAGE)
def cleanup(agent_id: str, display_name: str):
    # Exit-handler task. KFP forbids exit tasks from depending on other tasks,
    # so this works purely from pipeline params: only when a fresh temp engine
    # was deployed (agent_id empty) do we delete it, matching by the unique
    # display_name that resolve_agent deployed it under.
    if agent_id or not display_name:
        return

    import vertexai
    from vertexai import agent_engines

    from src.config import GCP_PROJECT_ID, GCP_REGION

    vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
    try:
        for eng in agent_engines.list():
            dn = getattr(eng, "display_name", None)
            if dn is None:
                dn = getattr(getattr(eng, "api_resource", None), "display_name", None)
            if dn != display_name:
                continue
            name = getattr(eng, "resource_name", None) or getattr(
                getattr(eng, "api_resource", None), "name", None
            )
            if name:
                agent_engines.delete(name, force=True)
                print(f"deleted temp engine: {name}")
    except Exception as e:
        print(f"cleanup skipped: {e}")


@dsl.component(base_image=IMAGE)
def optimize_agent(
    result: dsl.Output[dsl.Artifact],
    optimized_prompt: dsl.Output[dsl.Artifact],
    agent_opt_module: str = "src/agents/coordinator",
    sampler_config_path: str = "src/optimize/sampler_config.json",
    optimizer_config_path: str = "",
    experiment_id: str = "",
    agent_tag: str = "coordinator",
):
    """Run GEPA prompt optimization as a managed-pipeline task.

    Unlike the eval components (which score a *deployed* engine), this runs the
    agent *locally inside the container* via ``run_optimize`` — so the container
    needs the MCP servers reachable (their Cloud Run URLs are baked as env by the
    pipeline's ``_wire``). Emits the best optimized instruction as both a JSON
    result and a plain-text prompt artifact, and mirrors them to a deterministic
    GCS prefix so the outcome can be harvested and pasted back into the agent.
    """
    import json
    import os

    # The eval image COPYs the repo to /app; run_optimize resolves the agent
    # module + eval-set paths relative to CWD, so anchor there.
    if os.path.isdir("/app/src"):
        os.chdir("/app")

    from src.config import GCP_STAGING_BUCKET
    from src.optimize.run_optimize import run_optimize, summarize_gepa_result

    opt = run_optimize(
        agent_module_path=agent_opt_module,
        sampler_config_path=sampler_config_path,
        optimizer_config_path=optimizer_config_path or None,
        print_detailed=True,
    )

    summary = summarize_gepa_result(opt)
    instruction = summary["optimized_instruction"]

    payload = {
        "experiment_id": experiment_id,
        "agent": agent_tag,
        "agent_opt_module": agent_opt_module,
        "sampler_config_path": sampler_config_path,
        **summary,
    }
    with open(result.path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    with open(optimized_prompt.path, "w") as f:
        f.write(instruction or "")

    try:
        from google.cloud import storage

        client = storage.Client()
        bucket = client.bucket(GCP_STAGING_BUCKET)
        prefix = f"optimize-results/{experiment_id or 'adhoc'}/{agent_tag}"
        bucket.blob(f"{prefix}/result.json").upload_from_filename(result.path)
        bucket.blob(f"{prefix}/optimized_instruction.txt").upload_from_filename(
            optimized_prompt.path
        )
        print(f"uploaded optimize results to gs://{GCP_STAGING_BUCKET}/{prefix}/")
    except Exception as e:
        print(f"optimize result upload skipped: {e}")
