"""
Deploy ADK agents to Vertex AI Agent Runtime with identity, gateway, and Memory Bank.

Usage:
  # Deploy new agents
  uv run python -m src.deploy.deploy_agents router
  uv run python -m src.deploy.deploy_agents coordinator
  uv run python -m src.deploy.deploy_agents all

  # Update existing agents (uses engine IDs from .env)
  uv run python -m src.deploy.deploy_agents router --update
  uv run python -m src.deploy.deploy_agents coordinator --update
  uv run python -m src.deploy.deploy_agents all --update

Controlled by .env:
  - ENABLE_AGENT_IDENTITY=1 → sets SPIFFE identity
  - ENABLE_AGENT_GATEWAY=1 → attaches gateway (requires early-access)
"""

import os
from collections.abc import Callable
from typing import Any, TypedDict

import vertexai
from vertexai import agent_engines

from src.config import (
    AGENT_ANALYTICS_TABLE,
    AGENT_ENGINE_ID,
    AGENT_GATEWAY_EGRESS_PATH,
    AGENT_MODEL,
    AGENT_REGISTRY_LOCATION,
    BOOKING_MCP_SERVER,
    BOOKING_MCP_URL,
    BQ_AGENT_ANALYTICS_DATASET,
    CLASSIFIER_MODEL,
    COMPLEXITY_HIGH,
    COMPLEXITY_LOW,
    COMPLEXITY_THRESHOLD_HIGH,
    COORDINATOR_MODEL,
    DEPLOY_TAG,
    ENABLE_AGENT_ANALYTICS,
    ENABLE_MEMORY_BANK,
    ENABLE_MEMORY_PRELOAD_CACHE,
    ENABLE_SPAN_CONTENT_CAPTURE,
    EXPENSE_MCP_SERVER,
    EXPENSE_MCP_URL,
    EXPENSE_MODEL,
    FLASH_MODEL,
    GCP_PROJECT_ID,
    GCP_REGION,
    GCP_STAGING_BUCKET,
    HIGH_SPLIT,
    LITE_MODEL,
    MEDIUM_SPLIT,
    OPUS_MODEL,
    OTEL_ENV_VARS,
    PRO_MODEL,
    PROMPT_VARIANT,
    RESOURCE_LABELS,
    ROUTER_MODEL,
    SEARCH_MCP_SERVER,
    SEARCH_MCP_URL,
    SONNET_MODEL,
    TRAVEL_MODEL,
)

# Runtime dependency subset for the served Agent Engine. Keep the version floors
# aligned with pyproject.toml (the stack we test + build the eval image against);
# this hand-list exists only because the engine needs a trimmed set (no pytest,
# kfp, pandas, matplotlib, etc.). Deliberate deviations from pyproject:
#   * No `evaluation`/`eval` extras — offline eval runs in the eval-runner image /
#     Vertex pipeline, not in the served engine, and the evaluation extra caps
#     litellm (<1.86.0), conflicting with our litellm floor → unresolvable build.
#   * cloudpickle pinned <4 — Agent Engine serializes the app with cloudpickle.
#   * google-adk exact-pinned to the tested/locked version (see uv.lock) so the
#     runtime rebuild matches the ADK we pickle the app against locally — a
#     local-pickle↔runtime version skew can mis-load tools / mangle model calls.
REQUIREMENTS = [
    "google-cloud-aiplatform[adk,agent-engines]>=1.163.0",
    "google-genai>=2",
    "google-auth>=2.52.0",
    "google-adk[agent-identity]==2.6.3",
    "a2a-sdk>=1",
    "fastmcp>=2.0.0",
    "python-dotenv>=1.0.0",
    "litellm>=1.83.14",
    "pydantic>=2.12.5",
    "cloudpickle>=3.0,<4.0",
    # OTel instrumentation — Agent Engine auto-enables telemetry, but without
    # these the emitted spans carry no gen_ai.* prompt/response attributes, so
    # Online Evaluators (which score {prompt}/{response} from gen_ai spans)
    # silently produce zero results. google-genai is the one that unblocks eval.
    "opentelemetry-instrumentation-google-genai",
    "opentelemetry-instrumentation-grpc",
    "opentelemetry-instrumentation-httpx",
    # NB: the BigQuery Agent Analytics plugin's serving deps
    # (google-cloud-bigquery-storage / google-cloud-storage / pyarrow) are
    # DELIBERATELY NOT here. Task 1.4 proved content-logging fails on the managed
    # runtime (the plugin's before_run_callback errors in _ensure_schema_exists and
    # aborts every run — see docs/notes/agent-analytics-bigquery.md), so we don't
    # ship dead weight to the served image. The opt-in wiring (_analytics_plugin /
    # ENABLE_AGENT_ANALYTICS) is kept dormant; re-enabling it for real would require
    # adding these three back here AND resolving the runtime schema-setup failure.
    # "google-cloud-iamconnectorcredentials>=0.1.0",
]

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ENV_FILE = os.path.join(PROJECT_ROOT, ".env")


ENABLE_AGENT_IDENTITY = os.environ.get("ENABLE_AGENT_IDENTITY", "0") in ("1", "true")
ENABLE_AGENT_GATEWAY = os.environ.get("ENABLE_AGENT_GATEWAY", "0") in ("1", "true")


def _build_gateway_config() -> dict | None:
    """Build the agent_gateway_config dict for agent_engines.create().

    Requires ENABLE_AGENT_GATEWAY=1 in .env — gateway integration needs
    early-access activation on the GCP project. Without it, deploy fails
    with FAILED_PRECONDITION.
    """
    if not ENABLE_AGENT_GATEWAY or not AGENT_GATEWAY_EGRESS_PATH:
        return None
    return {"agent_to_anywhere_config": {"agent_gateway": AGENT_GATEWAY_EGRESS_PATH}}


def _runtime_engine_id() -> str:
    """The engine ID the Memory Bank / Session services must be scoped to.

    These builders run *inside the deployed container*, where the Agent Engine
    runtime injects the engine's OWN resource ID as ``GOOGLE_CLOUD_AGENT_ENGINE_ID``.
    That is the correct scope for a self-hosted Session/Memory store — an engine's
    sessions live on the engine itself.

    We must NOT bake ``config.AGENT_ENGINE_ID`` here: on a fresh ``create`` the
    engine's own ID does not exist yet, so ``AGENT_ENGINE_ID`` holds a stale/other
    engine, and scoping the session service to a different engine makes
    ``create_session`` fail at runtime ("Failed to create session"). Prefer the
    runtime-provided own-ID; fall back to ``AGENT_ENGINE_ID`` only for local/test
    runs where the runtime var is absent.
    """
    return os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID") or AGENT_ENGINE_ID


def _memory_service_builder():
    """Build a VertexAiMemoryBankService for use with AdkApp.

    Attached (via ``_build_app``) to memory-enabled agents so the deployed
    Agent Engine reads/writes Vertex Memory Bank rather than the default
    in-memory store — this is what makes cross-session recall persist.
    """
    from google.adk.memory import VertexAiMemoryBankService

    return VertexAiMemoryBankService(
        project=GCP_PROJECT_ID,
        location=GCP_REGION,
        agent_engine_id=_runtime_engine_id(),
    )


def _session_service_builder():
    """Build a VertexAiSessionService for use with AdkApp.

    Mirrors ``_memory_service_builder``: attached to memory-enabled agents so
    multi-turn sessions persist server-side on Vertex managed Sessions (the
    write side that ``save_memories_callback`` flushes into Memory Bank).
    """
    from google.adk.sessions import VertexAiSessionService

    return VertexAiSessionService(
        project=GCP_PROJECT_ID,
        location=GCP_REGION,
        agent_engine_id=_runtime_engine_id(),
    )


def _wants_memory(agent) -> bool:
    """True if the agent reads Memory Bank (holds a PreloadMemoryTool).

    Only agents that read/write Memory Bank (the coordinator) need the managed
    memory + session services; single-tier agents and the router do not, so
    they deploy unchanged.
    """
    from google.adk.tools.preload_memory_tool import PreloadMemoryTool

    tools = getattr(agent, "tools", None) or []
    return any(isinstance(t, PreloadMemoryTool) for t in tools)


def _analytics_plugin():
    """Return the BigQuery Agent Analytics plugin, or None when disabled.

    Opt-in via ``ENABLE_AGENT_ANALYTICS`` (default off). When enabled, the ADK
    ``BigQueryAgentAnalyticsPlugin`` runs at the runner level inside the served
    engine and streams full LLM requests/responses + tool calls to BigQuery via
    the Storage Write API — a model-neutral content path (Gemini AND Claude via
    LiteLlm) that is independent of the OTEL trace surface the managed runtime
    strips. Rows carry the trace_id, so they join back to Cloud Trace.

    The import is deferred so the disabled path (and unit tests) never touch the
    BigQuery client libraries. Requires the BigQuery Storage Write API + dataset
    IAM on the Agent Engine runtime SA — see docs/notes/agent-analytics-bigquery.md.
    """
    if not ENABLE_AGENT_ANALYTICS:
        return None
    from google.adk.plugins.bigquery_agent_analytics_plugin import (
        BigQueryAgentAnalyticsPlugin,
        BigQueryLoggerConfig,
    )

    return BigQueryAgentAnalyticsPlugin(
        project_id=GCP_PROJECT_ID,
        dataset_id=BQ_AGENT_ANALYTICS_DATASET,
        table_id=AGENT_ANALYTICS_TABLE,
        location="US",
        config=BigQueryLoggerConfig(view_prefix="v_", batch_size=50, queue_max_size=10000),
    )


def _build_app(agent):
    """Return the object to deploy for ``agent``.

    EVERY agent is wrapped in an ``AdkApp`` bound to a managed
    ``VertexAiSessionService`` scoped to the engine's OWN runtime id. Without it,
    the runtime's default wrapping cannot create the server-side sessions that
    ``stream_query`` requires, so the deployed engine fails every request with
    "Failed to create session" (the bug that silenced the router + leaf agents).
    Memory-enabled agents (the coordinator) additionally get the Memory Bank
    service so recall persists across sessions.

    When ``ENABLE_AGENT_ANALYTICS`` is set, the runner-level BigQuery analytics
    plugin is attached to every agent so full content is logged (see
    ``_analytics_plugin``). AdkApp forwards ``plugins`` to the Runner and
    deep-copies them on clone(), so they survive the deploy cycle.

    Structural OTEL tracing (the agent/tool/model span *tree* in Cloud Trace) is
    enabled by the managed runtime via the
    ``GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true`` env var set in
    ``_build_config``. That is enough for traces to *appear*, but it does NOT put
    prompt/response *content* on the spans: the managed AdkApp ``set_up()``
    hard-overwrites ``ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS`` at container start
    based *solely* on the deprecated ``enable_tracing`` template flag (not on the
    telemetry env var), forcing it ``"false"`` when the flag is absent — which is
    why every content-capture env var baked into the deploy spec was a no-op and
    the native Online Evaluators returned ``INSUFFICIENT_DATA``.

    ``ENABLE_SPAN_CONTENT_CAPTURE=1`` opts into ``AdkApp(enable_tracing=True)``,
    the one lever that opens that gate so ``call_llm`` spans carry the real
    ``gcp.vertex.agent.llm_request`` (system_instruction + contents) /
    ``llm_response``. Default OFF: the flag was previously removed after a deploy
    appeared to crash the worker mid-request (0 events streamed) — a signature
    matching the concurrent platform outage that crashed *all* fresh engines and
    no longer reproduces on the native-Gemini path. Enable it on the
    native-Gemini backbone. See docs/notes/online-eval-content-capture.md.
    """
    builders = {"session_service_builder": _session_service_builder}
    if _wants_memory(agent):
        builders["memory_service_builder"] = _memory_service_builder
    plugin = _analytics_plugin()
    plugins = [plugin] if plugin else None
    if ENABLE_SPAN_CONTENT_CAPTURE:
        builders["enable_tracing"] = True
    # ty can't track the heterogeneous **builders dict against AdkApp's typed
    # keyword params; the keys are validated by construction above.
    return agent_engines.AdkApp(agent=agent, plugins=plugins, **builders)  # ty: ignore[invalid-argument-type]


def _tagged_display_name(agent, tag: str | None = None) -> str:
    """Return the agent's console display name, suffixed with ``tag``.

    A ``--tag`` groups a deploy batch in the Agent Engine console
    (e.g. ``coordinator_agent_jt1`` / ``router_agent_jt1``). When no tag is
    given it defaults to ``DEPLOY_TAG`` (``jt1``), so display names match the
    rest of this operator's engines and a plain ``--update`` never drops it.
    This is distinct from the ``solution`` resource label.
    """
    tag = tag or DEPLOY_TAG
    return f"{agent.name}_{tag}" if tag else agent.name


def _build_config(
    agent, display_name: str | None = None, *, min_instances: int | None = None
) -> dict:
    """Build the deployment config dict used for both create and update.

    ``min_instances`` sets a keep-warm floor (Agent Engine ``min_instances``) so
    the engine never scales to zero. The default (None) preserves scale-to-zero;
    a floor of 1 avoids the idle cold-start/error-shaped-stream wedge that a demo
    engine can fall into when left idle (see the pre-demo readiness runbook).
    """
    env_vars = {
        **OTEL_ENV_VARS,
        "GCP_PROJECT_ID": GCP_PROJECT_ID,
        "GCP_REGION": GCP_REGION,
        "SEARCH_MCP_URL": SEARCH_MCP_URL,
        "BOOKING_MCP_URL": BOOKING_MCP_URL,
        "EXPENSE_MCP_URL": EXPENSE_MCP_URL,
        "AGENT_ENGINE_ID": AGENT_ENGINE_ID,
        "OPUS_MODEL": OPUS_MODEL,
        "SONNET_MODEL": SONNET_MODEL,
        "PRO_MODEL": PRO_MODEL,
        "LITE_MODEL": LITE_MODEL,
        "FLASH_MODEL": FLASH_MODEL,
        "COMPLEXITY_THRESHOLD_HIGH": str(COMPLEXITY_THRESHOLD_HIGH),
        # DOE factor env — baked into the engine so config-overridden variants
        # take effect at import time inside the deployed container.
        "AGENT_MODEL": AGENT_MODEL,
        "COORDINATOR_MODEL": COORDINATOR_MODEL,
        "TRAVEL_MODEL": TRAVEL_MODEL,
        "EXPENSE_MODEL": EXPENSE_MODEL,
        "ROUTER_MODEL": ROUTER_MODEL,
        # Complexity classifier model (router's before_agent_callback). Baked so a
        # deploy-env override reaches the container — a thinking model (e.g.
        # gemini-3.5-flash) spends its token budget on reasoning and returns empty
        # text, forcing classify_complexity's low-score fallback on every request
        # (so everything routes to the lite tier). A non-thinking model returns
        # real differentiated scores.
        "CLASSIFIER_MODEL": CLASSIFIER_MODEL,
        "COMPLEXITY_LOW": str(COMPLEXITY_LOW),
        "COMPLEXITY_HIGH": str(COMPLEXITY_HIGH),
        "MEDIUM_SPLIT": str(MEDIUM_SPLIT),
        "HIGH_SPLIT": str(HIGH_SPLIT),
        "PROMPT_VARIANT": PROMPT_VARIANT,
        "ENABLE_MEMORY_BANK": "1" if ENABLE_MEMORY_BANK else "0",
        "GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES": "false",
        "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
        "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "EVENT_ONLY",
        "GOOGLE_GENAI_USE_VERTEXAI": "1",
        "SEARCH_MCP_SERVER": SEARCH_MCP_SERVER,
        "BOOKING_MCP_SERVER": BOOKING_MCP_SERVER,
        "EXPENSE_MCP_SERVER": EXPENSE_MCP_SERVER,
        "AGENT_REGISTRY_LOCATION": AGENT_REGISTRY_LOCATION,
    }

    # Memory-preload cache (opt-in latency knob). The coordinator selects
    # CachingPreloadMemoryTool vs PreloadMemoryTool at import time from this flag,
    # so bake it into the container env — otherwise a runtime re-import inside the
    # managed runtime would rebuild the tool with the default (cache off) and the
    # deployed engine would silently not run the cache. Only baked when enabled so
    # default deploys keep byte-identical env.
    if ENABLE_MEMORY_PRELOAD_CACHE:
        env_vars["ENABLE_MEMORY_PRELOAD_CACHE"] = "1"

    # Model Armor template names for server-side screening (read by
    # src/armor/config.get_model_armor_config). Only bake when explicitly set so
    # the deployed engine falls back to the project/region-derived defaults
    # rather than being overridden with an empty string.
    for armor_var in ("MODEL_ARMOR_PROMPT_TEMPLATE", "MODEL_ARMOR_RESPONSE_TEMPLATE"):
        armor_val = os.environ.get(armor_var)
        if armor_val:
            env_vars[armor_var] = armor_val

    config = {
        "staging_bucket": f"gs://{GCP_STAGING_BUCKET}",
        "requirements": REQUIREMENTS,
        "display_name": display_name or agent.name,
        "env_vars": env_vars,
        "extra_packages": ["src"],
        "labels": dict(RESOURCE_LABELS),
    }

    if min_instances is not None:
        config["min_instances"] = int(min_instances)
        print(f"  Keep-warm: min_instances={int(min_instances)}")

    if ENABLE_AGENT_IDENTITY:
        config["identity_type"] = "AGENT_IDENTITY"
        print("  Identity: AGENT_IDENTITY (SPIFFE-based)")
    else:
        print("  Identity: default (set ENABLE_AGENT_IDENTITY=1 to enable)")

    gateway_config = _build_gateway_config()
    if gateway_config:
        config["agent_gateway_config"] = gateway_config
        print(f"  Gateway: egress={AGENT_GATEWAY_EGRESS_PATH}")
    else:
        if not ENABLE_AGENT_GATEWAY:
            print("  Gateway: disabled (set ENABLE_AGENT_GATEWAY=1 to enable)")
        else:
            print("  Gateway: not configured (set AGENT_GATEWAY_EGRESS_PATH)")

    return config


def _get_client():
    return vertexai.Client(
        project=GCP_PROJECT_ID,
        location=GCP_REGION,
        # http_options=dict(api_version="v1beta1"),
    )


def deploy_agent(
    agent, display_name: str | None = None, *, min_instances: int | None = None
) -> str:
    """Create a new agent on Agent Runtime."""
    os.chdir(PROJECT_ROOT)
    print(f"\n--- Creating {agent.name} ---")
    config = _build_config(agent, display_name, min_instances=min_instances)

    remote = _get_client().agent_engines.create(agent=_build_app(agent), config=config)
    resource_name = getattr(remote, "resource_name", None) or remote.api_resource.name
    print(f"  Created: {resource_name}")
    return resource_name


def update_agent(
    agent, engine_id: str, display_name: str | None = None, *, min_instances: int | None = None
) -> str:
    """Update an existing agent on Agent Runtime."""
    os.chdir(PROJECT_ROOT)
    # Accept bare ID or full resource name
    if not engine_id.startswith("projects/"):
        engine_id = f"projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/reasoningEngines/{engine_id}"
    print(f"\n--- Updating {agent.name} ({engine_id.split('/')[-1]}) ---")
    config = _build_config(agent, display_name, min_instances=min_instances)

    remote = _get_client().agent_engines.update(
        name=engine_id,
        agent=_build_app(agent),
        config=config,
    )
    resource_name = getattr(remote, "resource_name", None) or remote.api_resource.name
    print(f"  Updated: {resource_name}")
    return resource_name


COORDINATOR_ENGINE_ID = os.environ.get("COORDINATOR_AGENT_ID", "")
ROUTER_ENGINE_ID_ENV = os.environ.get("ROUTER_ENGINE_ID", os.environ.get("AGENT_ENGINE_ID", ""))
LITE_ENGINE_ID = os.environ.get("LITE_ENGINE_ID", "")
FLASH_ENGINE_ID = os.environ.get("FLASH_ENGINE_ID", "")
PRO_ENGINE_ID = os.environ.get("PRO_ENGINE_ID", "")
SONNET_ENGINE_ID = os.environ.get("SONNET_ENGINE_ID", "")
OPUS_ENGINE_ID = os.environ.get("OPUS_ENGINE_ID", "")


class _AgentSet(TypedDict):
    loader: Callable[[], Any]
    engine_id: str | None
    env_var: str


AGENT_SETS: dict[str, _AgentSet] = {
    "coordinator": {
        "loader": lambda: (
            __import__(
                "src.agents.coordinator_agent", fromlist=["coordinator_agent"]
            ).coordinator_agent
        ),
        "engine_id": COORDINATOR_ENGINE_ID,
        "env_var": "COORDINATOR_AGENT_ID",
    },
    "router": {
        "loader": lambda: __import__("src.router.agents", fromlist=["router_agent"]).router_agent,
        "engine_id": ROUTER_ENGINE_ID_ENV,
        "env_var": "ROUTER_ENGINE_ID",
    },
    "lite": {
        "loader": lambda: __import__("src.agents.lite_agent", fromlist=["lite_agent"]).lite_agent,
        "engine_id": LITE_ENGINE_ID,
        "env_var": "LITE_ENGINE_ID",
    },
    "flash": {
        "loader": lambda: (
            __import__("src.agents.flash_agent", fromlist=["flash_agent"]).flash_agent
        ),
        "engine_id": FLASH_ENGINE_ID,
        "env_var": "FLASH_ENGINE_ID",
    },
    "pro": {
        "loader": lambda: __import__("src.agents.pro_agent", fromlist=["pro_agent"]).pro_agent,
        "engine_id": PRO_ENGINE_ID,
        "env_var": "PRO_ENGINE_ID",
    },
    "sonnet": {
        "loader": lambda: (
            __import__("src.agents.sonnet_agent", fromlist=["sonnet_agent"]).sonnet_agent
        ),
        "engine_id": SONNET_ENGINE_ID,
        "env_var": "SONNET_ENGINE_ID",
    },
    "opus": {
        "loader": lambda: __import__("src.agents.opus_agent", fromlist=["opus_agent"]).opus_agent,
        "engine_id": OPUS_ENGINE_ID,
        "env_var": "OPUS_ENGINE_ID",
    },
}


def _update_env_file(env_var: str, value: str):
    """Update or append a variable in the .env file."""
    engine_id = value.split("/")[-1]
    lines = []
    found = False
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            lines = f.readlines()
        for i, line in enumerate(lines):
            if line.startswith(f"{env_var}="):
                lines[i] = f"{env_var}={engine_id}\n"
                found = True
                break
    if not found:
        lines.append(f"{env_var}={engine_id}\n")
    with open(ENV_FILE, "w") as f:
        f.writelines(lines)
    print(f"  .env updated: {env_var}={engine_id}")


def run_deploy(
    agent_set: str = "all",
    update: bool = False,
    tag: str | None = None,
    *,
    min_instances: int | None = None,
) -> dict[str, str]:
    """Deploy or update agents and return a map of name → resource name.

    Args:
        agent_set: "coordinator", "router", or "all" (default).
        update: If True, update existing agents using engine IDs from .env.
                If False, create new agents.
        tag: Optional suffix appended to each agent's console display name
             (e.g. tag="demo1" → "coordinator_agent_demo1") to keep a deploy
             batch grouped in the Agent Engine console.
        min_instances: Keep-warm floor (Agent Engine ``min_instances``) applied
             to every agent in this batch. None (default) preserves each
             engine's existing scaling (scale-to-zero on create); a floor of
             1-2 avoids idle cold-start / empty-at-200 streams.
    """
    vertexai.init(
        project=GCP_PROJECT_ID, location=GCP_REGION, staging_bucket=f"gs://{GCP_STAGING_BUCKET}"
    )

    if agent_set == "all":
        sets = list(AGENT_SETS.keys())
    else:
        sets = [s.strip() for s in agent_set.split(",")]

    deployed = {}
    for name in sets:
        entry = AGENT_SETS.get(name)
        if not entry:
            print(f"  Unknown agent set: {name}. Available: {list(AGENT_SETS)}")
            continue
        agent = entry["loader"]()
        display_name = _tagged_display_name(agent, tag)

        if update:
            engine_id = entry["engine_id"]
            if not engine_id:
                print(f"  No engine ID for {name} — set {entry['env_var']} in .env")
                continue
            deployed[agent.name] = update_agent(
                agent, engine_id, display_name, min_instances=min_instances
            )
        else:
            resource_name = deploy_agent(agent, display_name, min_instances=min_instances)
            deployed[agent.name] = resource_name
            _update_env_file(entry["env_var"], resource_name)
            # Durable fix: the coordinator IS the default engine. Keep
            # AGENT_ENGINE_ID pointed at it so config-derived client-side
            # defaults (a2a url, metric labels) never drift to a stale engine.
            if name == "coordinator":
                _update_env_file("AGENT_ENGINE_ID", resource_name)

    return deployed


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Deploy or update ADK agents on Agent Engine")
    parser.add_argument(
        "agent_set", nargs="?", default="all", help="coordinator, router, or all (default: all)"
    )
    parser.add_argument(
        "--update", action="store_true", help="Update existing agents instead of creating new ones"
    )
    parser.add_argument(
        "--tag",
        default=None,
        help="Suffix appended to each agent's console display name "
        "(e.g. --tag demo1 → coordinator_agent_demo1) to group a deploy batch",
    )
    parser.add_argument(
        "--min-instances",
        type=int,
        default=None,
        help="Keep-warm floor (Agent Engine min_instances) so the engine never "
        "scales to zero — avoids idle cold-start / empty-at-200 streams "
        "(e.g. --min-instances 1). Default: unset (preserves existing scaling).",
    )
    args = parser.parse_args()

    deployed = run_deploy(
        agent_set=args.agent_set,
        update=args.update,
        tag=args.tag,
        min_instances=args.min_instances,
    )
    print("\n=== Agent Resource Names ===")
    for name, resource in deployed.items():
        print(f"  {name}: {resource}")
