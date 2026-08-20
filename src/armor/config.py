"""Agent Armor configuration — Model Armor templates and guardrail callbacks.

Single source of truth for the guardrail: both the coordinator and the router
import this module (the router previously carried a duplicate ``src/router/armor.py``).

Provides three layers of protection:
1. ModelArmorConfig: Server-side screening via Model Armor templates (prompt injection,
   content safety, sensitive data, malicious URLs)
2. ``input_guardrail_callback``: pure client-side input validation (blocklist,
   length limits) with a Content|None contract — trivially testable, no side effects.
3. ``guardrail_with_telemetry``: a thin wrapper that runs the pure guardrail and,
   on a block, emits demo observability (an OTel ``guardrail.blocked`` span event
   plus a ``custom.googleapis.com/agent_armor/blocked`` metric). Telemetry is
   fully guarded so a metric/OTel failure NEVER changes the guardrail's return.
"""

import os
import re

from google.genai.types import GenerateContentConfig, ModelArmorConfig, ThinkingConfig
from opentelemetry import trace

from src import config
from src.config import GCP_PROJECT_ID, GCP_REGION
from src.models.afc import with_afc_disabled


def get_model_armor_config() -> ModelArmorConfig:
    """Build ModelArmorConfig from environment or defaults."""
    prompt_template = os.environ.get(
        "MODEL_ARMOR_PROMPT_TEMPLATE",
        f"projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/templates/geap-workshop-prompt",
    )
    response_template = os.environ.get(
        "MODEL_ARMOR_RESPONSE_TEMPLATE",
        f"projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/templates/geap-workshop-response",
    )
    return ModelArmorConfig(
        prompt_template_name=prompt_template,
        response_template_name=response_template,
    )


def _is_regional_gemini(model: str | None) -> bool:
    """True for Gemini-2.x / ``models/`` ids that serve on the regional endpoint.

    These are the only backbones where the region-scoped Model Armor templates are
    honored natively (mirrors the Gemini-2.x branch in ``config.resolve_model``).
    """
    return bool(model) and model.startswith(("gemini-2", "models/"))


def server_side_armor_enabled(model: str | None) -> bool:
    """True when ``get_armored_generate_config`` actually attaches Model Armor.

    Exactly the gate applied below, named and exported so callers (e.g. the
    coordinator publishing ``armor.server_side`` on its request span) can report
    which security layers are live without re-deriving the family check.
    """
    return _is_regional_gemini(model)


def get_armored_generate_config(model: str | None = None) -> GenerateContentConfig:
    """GenerateContentConfig with server-side Model Armor only where it is honored.

    Model Armor templates are region-scoped and enforced natively on the Gemini 2.x
    path. Gemini 3.x runs on the global endpoint (no template support -> 400
    TEMPLATE_NOT_FOUND) and Claude runs via LiteLlm, so server-side armor is omitted
    for both; the client-side guardrail (``guardrail_with_telemetry``) is the
    guaranteed enforcement layer. See docs/notes/model-armor-security-dashboard.md.

    On the same regional-Gemini path we also attach the opt-in latency knobs
    (``COORDINATOR_THINKING_BUDGET`` / ``COORDINATOR_MAX_OUTPUT_TOKENS``) when set —
    uncapped default "thinking" dominates the coordinator's time-to-first-token
    (see docs/notes/coordinator-latency-attribution.md). Unset knobs preserve the
    prior behavior exactly. They are gated to the regional path because Gemini-3
    (native/global) and Claude (LiteLlm) resolve generation config differently; the
    probe backbone that carries the latency is regional gemini-2.5-flash.

    Both branches disable google-genai automatic function calling: AFC is on by
    default and ADK copies this config verbatim onto the request, which logged an
    "AFC is enabled" INFO per call plus a per-process WARNING. ADK does its own
    function calling, so this is behaviour-preserving — see
    docs/notes/genai-afc-warning.md.
    """
    if not _is_regional_gemini(model):
        return with_afc_disabled(GenerateContentConfig())

    budget = config.COORDINATOR_THINKING_BUDGET
    thinking = ThinkingConfig(thinking_budget=budget) if budget is not None else None
    return with_afc_disabled(
        GenerateContentConfig(
            model_armor_config=get_model_armor_config(),
            thinking_config=thinking,
            max_output_tokens=config.COORDINATOR_MAX_OUTPUT_TOKENS,
        )
    )


# --- Client-side guardrail callback ---

MAX_INPUT_LENGTH = 4000

BLOCKED_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(a|an)\s+", re.IGNORECASE),
    re.compile(r"system\s*:\s*", re.IGNORECASE),
    re.compile(r"<\s*/?script", re.IGNORECASE),
]

REJECTION_MESSAGE = "I'm sorry, I can't process that request. Please rephrase your question about travel or expenses."

# Coarse, low-cardinality block reasons (used as metric labels + span-event attrs).
REASON_TOO_LONG = "input_too_long"
REASON_BLOCKED_PATTERN = "blocked_pattern"

# Metric type emitted (once per block) so a governance BLOCK is observable in
# Cloud Monitoring. Bare here; MetricsWriter normalizes to custom.googleapis.com/.
ARMOR_BLOCKED_METRIC = "agent_armor/blocked"


def _extract_user_message(callback_context) -> str:
    """Pull the concatenated user text out of a callback context (Content|str)."""
    from google.genai.types import Content

    context = callback_context
    user_message = ""
    if context is not None and getattr(context, "user_content", None):
        user_content = context.user_content
        if isinstance(user_content, Content):
            for part in user_content.parts or []:
                if part.text:
                    user_message += part.text
        elif isinstance(user_content, str):
            user_message = user_content
    return user_message


def classify_block(user_message: str) -> str | None:
    """Return WHY a message would be blocked, or None if it passes.

    Kept side-effect free so both ``input_guardrail_callback`` (for its return
    Content) and ``guardrail_with_telemetry`` (for its reason label) can share it.
    """
    if not user_message:
        return None
    if len(user_message) > MAX_INPUT_LENGTH:
        return REASON_TOO_LONG
    for pattern in BLOCKED_PATTERNS:
        if pattern.search(user_message):
            return REASON_BLOCKED_PATTERN
    return None


def input_guardrail_callback(callback_context=None, **kwargs):
    """before_agent_callback that rejects suspicious or oversized inputs.

    Pure validator: returns a Content rejection if the input fails validation, or
    None to proceed. No telemetry side effects — see ``guardrail_with_telemetry``
    for the observability-emitting wrapper. This runs client-side before the
    request reaches Model Armor's server-side filters.
    """
    from google.genai.types import Content, Part

    user_message = _extract_user_message(callback_context)
    reason = classify_block(user_message)
    if reason == REASON_TOO_LONG:
        return Content(
            parts=[
                Part(
                    text=f"Input too long ({len(user_message)} chars, max {MAX_INPUT_LENGTH}). Please shorten your request."
                )
            ]
        )
    if reason == REASON_BLOCKED_PATTERN:
        return Content(parts=[Part(text=REJECTION_MESSAGE)])
    return None


def _emit_block_telemetry(reason: str, metrics_writer=None) -> None:
    """Emit a span event + metric for a governance block.

    Each side is independently guarded so a failure in one (or in credentials for
    the metric client) never propagates — the guardrail's decision must stand
    regardless of whether observability succeeds.
    """
    try:
        span = trace.get_current_span()
        if span is not None and span.is_recording():
            span.add_event("guardrail.blocked", {"guardrail.reason": reason})
    except Exception:
        pass

    try:
        writer = metrics_writer
        if writer is None:
            from src.observability.metrics import MetricsWriter

            writer = MetricsWriter()
        writer.write_gauge(ARMOR_BLOCKED_METRIC, 1, labels={"reason": reason})
    except Exception:
        pass


def guardrail_with_telemetry(callback_context=None, metrics_writer=None, **kwargs):
    """Telemetry-wrapping guardrail — the coordinator's before_agent_callback.

    Runs the pure ``input_guardrail_callback`` and, on a block (non-None), adds a
    ``guardrail.blocked`` OTel span event and increments the ``agent_armor/blocked``
    metric. Returns exactly what the pure guardrail returned; telemetry is fully
    guarded so it can never change the decision. ``metrics_writer`` is injectable
    for tests.
    """
    result = input_guardrail_callback(callback_context=callback_context, **kwargs)
    if result is not None:
        reason = classify_block(_extract_user_message(callback_context)) or "blocked"
        _emit_block_telemetry(reason, metrics_writer=metrics_writer)
    return result
