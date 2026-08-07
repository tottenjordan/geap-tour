"""KFP v2 pipeline that runs GEPA prompt optimization as a managed job.

The sibling of ``eval_pipeline``: same env-injection pattern, same runner image,
submitted the same non-blocking way (``src.pipelines.submit_optimize``). The one
structural difference is that GEPA runs the agent *locally inside the component*
(``LocalEvalSampler``), so the component must reach the MCP servers — their Cloud
Run URLs are baked here alongside the Agent Registry names.

Deployment/factor env is read from ``os.environ`` at compile time and baked onto
the task via ``set_env_variable`` (KFP forbids passing a pipeline *parameter* to
it). ``submit_optimize`` recompiles per run, so a launcher that sets model/prompt
env (e.g. the DOE fan-out) gets that variant baked — making the optimization
itself DOE-fannable (optimize under flash vs pro, etc.).
"""

import os

from kfp import dsl

from src.config import (
    BOOKING_MCP_SERVER,
    BOOKING_MCP_URL,
    EXPENSE_MCP_SERVER,
    EXPENSE_MCP_URL,
    GCP_PROJECT_ID,
    SEARCH_MCP_SERVER,
    SEARCH_MCP_URL,
)
from src.pipelines import components as c

# GEPA runs the agent locally (in-container), so the component needs BOTH the
# Agent Registry names and the Cloud Run URL fallbacks reachable, AND it runs the
# agent's model client itself (unlike the eval components, which score a deployed
# engine).
#
# Two env vars mirror the deployed-engine config (deploy_agents._build_config):
#   * GOOGLE_GENAI_USE_VERTEXAI=1 — use Vertex, not the Developer API.
#   * GOOGLE_CLOUD_PROJECT=<hybrid-vertex> — a Vertex Pipelines custom job's
#     metadata-server project is the managed *tenant* project (…-tp), NOT ours,
#     so predict calls land there and 403 with PERMISSION_DENIED on
#     aiplatform.endpoints.predict. Pinning the project routes them back to ours.
#
# Deliberately NOT set: GOOGLE_CLOUD_LOCATION. resolve_model() wraps 3.x/Claude
# models in LiteLlm(vertex_location="global") and leaves 2.x models regional;
# a single GOOGLE_CLOUD_LOCATION env overrides that per-model choice and forces
# everything to one region — which 404s the global-only 3.x agent models.
_RUNTIME_ENV = {
    "SEARCH_MCP_SERVER": SEARCH_MCP_SERVER,
    "BOOKING_MCP_SERVER": BOOKING_MCP_SERVER,
    "EXPENSE_MCP_SERVER": EXPENSE_MCP_SERVER,
    "SEARCH_MCP_URL": SEARCH_MCP_URL,
    "BOOKING_MCP_URL": BOOKING_MCP_URL,
    "EXPENSE_MCP_URL": EXPENSE_MCP_URL,
    "GOOGLE_GENAI_USE_VERTEXAI": "1",
    "GOOGLE_CLOUD_PROJECT": GCP_PROJECT_ID,
}

# Factor env baked at compile time so a DOE launcher can optimize a config
# variant (e.g. base model tier). Absent keys fall back to src.config defaults.
_FACTOR_ENV_KEYS = (
    "AGENT_MODEL",
    "COORDINATOR_MODEL",
    "TRAVEL_MODEL",
    "EXPENSE_MODEL",
    "PROMPT_VARIANT",
)
_FACTOR_ENV = {k: os.environ[k] for k in _FACTOR_ENV_KEYS if k in os.environ}


def _wire(task):
    """Bake deployment + factor env onto a task as static env vars."""
    for key, value in {**_RUNTIME_ENV, **_FACTOR_ENV}.items():
        task.set_env_variable(key, value)
    return task


@dsl.pipeline(
    name="geap-optimize-pipeline",
    pipeline_root="gs://geap-tour-staging-v2/pipeline-root",
)
def optimize_pipeline(
    agent_opt_module: str = "src/agents/coordinator",
    sampler_config_path: str = "src/optimize/sampler_config.json",
    optimizer_config_path: str = "",
    experiment_id: str = "",
    agent_tag: str = "coordinator",
):
    opt = _wire(
        c.optimize_agent(
            agent_opt_module=agent_opt_module,
            sampler_config_path=sampler_config_path,
            optimizer_config_path=optimizer_config_path,
            experiment_id=experiment_id,
            agent_tag=agent_tag,
        )
    )
    # GEPA is heavier and longer than a single eval: give it headroom and never
    # auto-retry (a retry would re-run the full optimization).
    opt.set_cpu_limit("4").set_memory_limit("16G")
    opt.set_retry(num_retries=0)
