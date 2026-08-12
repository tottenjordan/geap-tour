"""Global configuration — GCP project settings, MCP server URLs, model configs, and eval params."""

import os

from dotenv import load_dotenv
from google.adk.models.lite_llm import LiteLlm

load_dotenv()

GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "hybrid-vertex")
PROJECT_NUMBER = os.environ.get("PROJECT_NUMBER", "")
GCP_REGION = os.environ.get("GCP_REGION", "us-central1")
GCP_STAGING_BUCKET = os.environ.get("GCP_STAGING_BUCKET", f"{GCP_PROJECT_ID}-geap-staging")
AGENT_GATEWAY_PATH = os.environ.get("AGENT_GATEWAY_PATH", "")
AGENT_GATEWAY_EGRESS_PATH = os.environ.get("AGENT_GATEWAY_EGRESS_PATH", "")

# Default resource label stamped onto every GCP resource we create, so demo
# assets are filterable/attributable. Override the value with SOLUTION_LABEL.
RESOURCE_LABELS = {"solution": os.environ.get("SOLUTION_LABEL", "geap-tour")}


def resource_labels_gcloud() -> str:
    """RESOURCE_LABELS as a gcloud --labels value: comma-joined key=value."""
    return ",".join(f"{k}={v}" for k, v in RESOURCE_LABELS.items())


def resource_labels_bq_flags() -> list[str]:
    """RESOURCE_LABELS as repeated `bq` --label key:value flags."""
    flags = []
    for k, v in RESOURCE_LABELS.items():
        flags += ["--label", f"{k}:{v}"]
    return flags

SEARCH_MCP_URL = os.environ.get("SEARCH_MCP_URL", "http://localhost:8001/mcp")
BOOKING_MCP_URL = os.environ.get("BOOKING_MCP_URL", "http://localhost:8002/mcp")
EXPENSE_MCP_URL = os.environ.get("EXPENSE_MCP_URL", "http://localhost:8003/mcp")

# Agent Registry — MCP server resource names (global location)
AGENT_REGISTRY_LOCATION = os.environ.get("AGENT_REGISTRY_LOCATION", "us-central1")
SEARCH_MCP_SERVER = os.environ.get("SEARCH_MCP_SERVER", "")
BOOKING_MCP_SERVER = os.environ.get("BOOKING_MCP_SERVER", "")
EXPENSE_MCP_SERVER = os.environ.get("EXPENSE_MCP_SERVER", "")

# Fallback: map Agent Registry server names → Cloud Run URLs
MCP_SERVER_URLS = {
    SEARCH_MCP_SERVER: SEARCH_MCP_URL,
    BOOKING_MCP_SERVER: BOOKING_MCP_URL,
    EXPENSE_MCP_SERVER: EXPENSE_MCP_URL,
}

OTEL_ENV_VARS = {
    "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_latest_experimental",
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "EVENT_ONLY",
}

AGENT_MODEL = os.environ.get("AGENT_MODEL", "gemini-3.5-flash")

# Per-agent model overrides (default to the shared AGENT_MODEL; overridable for DOE experiments)
COORDINATOR_MODEL = os.environ.get("COORDINATOR_MODEL", AGENT_MODEL)
TRAVEL_MODEL = os.environ.get("TRAVEL_MODEL", AGENT_MODEL)
EXPENSE_MODEL = os.environ.get("EXPENSE_MODEL", AGENT_MODEL)

# Prompt variant toggle: "gepa" (optimized, default) or "baseline"
PROMPT_VARIANT = os.environ.get("PROMPT_VARIANT", "gepa")


def resolve_model(model_str: str):
    """Resolve model string to an ADK-compatible model.

    Gemini 2.x models work in regional endpoints — pass as plain strings.
    Gemini 3.x and Claude models require location=global, so they are
    wrapped with LiteLLM which supports per-model location.
    """
    if model_str.startswith(("gemini-2", "models/")):
        return model_str
    if not model_str.startswith("vertex_ai/"):
        model_str = f"vertex_ai/{model_str}"
    return LiteLlm(model=model_str, vertex_location="global")

# Multi-model router (5-tier: lite → flash → pro → sonnet → opus)
LITE_MODEL = os.environ.get("LITE_MODEL", "gemini-3.1-flash-lite")
FLASH_MODEL = os.environ.get("FLASH_MODEL", "gemini-3.5-flash")
PRO_MODEL = os.environ.get("PRO_MODEL", "gemini-3.1-pro-preview")
SONNET_MODEL = os.environ.get("SONNET_MODEL", "claude-sonnet-4-6")
OPUS_MODEL = os.environ.get("OPUS_MODEL", "claude-opus-4-6")

# Router model (defaults to LITE_MODEL; must be defined after LITE_MODEL so the default resolves)
ROUTER_MODEL = os.environ.get("ROUTER_MODEL", LITE_MODEL)

# Router complexity boundaries (overridable for DOE).
# Defaults adopted from the DOE screening doe-screening-20260812-073603: the
# "aggressive_savings" cut-points won +26pp cost savings (68.7% -> 94.7%) for a
# ~0.04 quality dip and no other factor moved quality above eval noise. See
# docs/notes/doe-router-boundaries-inert.md and the screening report.
COMPLEXITY_LOW = float(os.environ.get("COMPLEXITY_LOW", "0.44"))
COMPLEXITY_HIGH = float(os.environ.get("COMPLEXITY_HIGH", "0.80"))
MEDIUM_SPLIT = float(os.environ.get("MEDIUM_SPLIT", "0.60"))
HIGH_SPLIT = float(os.environ.get("HIGH_SPLIT", "0.95"))

# Backwards-compat alias: still imported by src/deploy/deploy_agents.py
COMPLEXITY_THRESHOLD_HIGH = COMPLEXITY_HIGH

CLASSIFIER_MODEL = os.environ.get("CLASSIFIER_MODEL", "gemini-3.5-flash")
SIMULATOR_MODEL = os.environ.get("SIMULATOR_MODEL", "gemini-2.5-flash")

# Evaluation
EVAL_OUTPUT_DIR = os.environ.get("EVAL_OUTPUT_DIR", "eval_outputs")
BQ_EVAL_DATASET = os.environ.get("BQ_EVAL_DATASET", "geap_workshop_logs")
AGENT_ENGINE_ID = os.environ.get("AGENT_ENGINE_ID", "2479350891879071744")
ROUTER_ENGINE_ID = os.environ.get("ROUTER_ENGINE_ID", "6023683798619652096")

# A2A (Agent-to-Agent) — preview-optional. Identity for the coordinator's
# published agent card and the derived A2A endpoint.
A2A_AGENT_NAME = os.environ.get("A2A_AGENT_NAME", "coordinator_agent")
A2A_AGENT_VERSION = os.environ.get("A2A_AGENT_VERSION", "1.0.0")


def coordinator_a2a_url() -> str:
    """Base A2A endpoint URL for the deployed coordinator.

    Prefers an explicit ``A2A_ENDPOINT_URL`` override; otherwise derives the
    Agent Engine (reasoning engine) resource URL from the configured project,
    region, and engine id. The agent-card well-known path is appended by the
    A2A client, not here.
    """
    override = os.environ.get("A2A_ENDPOINT_URL")
    if override:
        return override
    return (
        f"https://{GCP_REGION}-aiplatform.googleapis.com/v1/"
        f"projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/"
        f"reasoningEngines/{AGENT_ENGINE_ID}"
    )


def disable_pyopenssl():
    """Neutralize pyopenssl 26.x's context-reuse guard.

    pyopenssl 26.x wraps Context methods with _require_not_used, which
    raises ValueError when concurrent requests mutate a reused SSL context.
    We can't remove pyopenssl (google-auth mTLS needs it), so we unwrap
    all guarded methods back to their originals via __wrapped__.
    """
    try:
        import OpenSSL.SSL as _ssl
        for attr in dir(_ssl.Context):
            method = getattr(_ssl.Context, attr, None)
            if callable(method) and hasattr(method, "__wrapped__"):
                setattr(_ssl.Context, attr, method.__wrapped__)
    except ImportError:
        pass
