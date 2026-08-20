"""Prompt complexity classifier using a lightweight model as a micro-judge.

Classifies prompts into 3 logical tiers, with score-based model selection
within the medium and high tiers:

  low    (0.0-0.30)  → LITE_MODEL
  medium (0.30-0.60) → FLASH_MODEL (0.30-0.45) or SONNET_MODEL (0.45-0.60)
  high   (0.60-1.0)  → PRO_MODEL (0.60-0.80) or OPUS_MODEL (0.80-1.0)
"""

import json
import os
from dataclasses import dataclass

from google import genai
from google.genai.types import GenerateContentConfig

from src.config import (
    CLASSIFIER_MODEL,
    COMPLEXITY_HIGH,
    COMPLEXITY_LOW,
    FLASH_MODEL,
    GCP_PROJECT_ID,
    HIGH_SPLIT,
    LITE_MODEL,
    MEDIUM_SPLIT,
    OPUS_MODEL,
    PRO_MODEL,
    SONNET_MODEL,
)
from src.models.afc import with_afc_disabled

# Newer Gemini models (3.x) are only available via location=global
CLASSIFIER_LOCATION = os.environ.get("CLASSIFIER_LOCATION", "global")

CLASSIFIER_PROMPT_TEMPLATE = (
    "Rate the complexity of this user prompt on a 0-1 scale.\n\n"
    "Criteria:\n"
    "- 0.0-0.29: Simple — single intent, direct lookup, one tool call, or a single action "
    '(e.g. "what is the meal limit?", "find hotels in Miami", "book flight FL001")\n'
    "- 0.30-0.59: Moderate — 2 related intents, comparison across options, or multi-step lookup "
    '(e.g. "compare flights by airline", "search hotels then check policy", "check two policy categories")\n'
    "- 0.60-1.0: Complex — 3+ intents, cross-domain analysis, multi-step planning, "
    "budget optimization, or strategic synthesis "
    '(e.g. "plan a multi-city trip with budget constraints", "review expenses and submit new ones")\n\n'
    "Scoring guidance:\n"
    "- Single lookups and simple bookings: 0.0-0.29.\n"
    "- Any comparison or 2-tool task: 0.30-0.59.\n"
    "- 3+ distinct tasks or cross-domain analysis: 0.60-0.79.\n"
    "- Team planning, budget optimization, or multi-city trips: 0.80-1.0.\n\n"
    'Return JSON with keys "score" (float) and "reason" (one sentence).\n\n'
    "Prompt: {prompt}"
)


@dataclass
class ComplexityResult:
    level: str
    score: float
    reason: str


# 3 logical tiers with sub-tiers for model selection.
# Boundaries are sourced from src.config so DOE experiments can override them
# via env vars (COMPLEXITY_LOW/HIGH, MEDIUM_SPLIT, HIGH_SPLIT).
THRESHOLDS = [COMPLEXITY_LOW, COMPLEXITY_HIGH]
LEVELS = ["low", "medium", "high"]

# Within-tier model selection boundaries (below → cheaper tier, above → pricier)
# MEDIUM_SPLIT: FLASH_MODEL vs SONNET_MODEL; HIGH_SPLIT: PRO_MODEL vs OPUS_MODEL


def _score_to_level(score: float) -> str:
    for threshold, level in zip(THRESHOLDS, LEVELS, strict=False):
        if score < threshold:
            return level
    return LEVELS[-1]


def score_to_model_tier(score: float) -> str:
    """Map a complexity score to a specific model tier for routing.

    Returns one of: 'lite', 'flash', 'sonnet', 'pro', 'opus'
    """
    if score < THRESHOLDS[0]:
        return "lite"
    elif score < MEDIUM_SPLIT:
        return "flash"
    elif score < THRESHOLDS[1]:
        return "sonnet"
    elif score < HIGH_SPLIT:
        return "pro"
    else:
        return "opus"


# Maps a routing tier to the concrete model id it dispatches to. Sourced from
# src.config so DOE model-override env vars are reflected here (and in traces).
_TIER_TO_MODEL = {
    "lite": LITE_MODEL,
    "flash": FLASH_MODEL,
    "sonnet": SONNET_MODEL,
    "pro": PRO_MODEL,
    "opus": OPUS_MODEL,
}


def tier_to_model(tier: str) -> str:
    """Return the concrete model id a routing tier dispatches to."""
    return _TIER_TO_MODEL.get(tier, LITE_MODEL)


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["score", "reason"],
}


_CLASSIFIER_CLIENT: genai.Client | None = None


def _classifier_client() -> genai.Client:
    """Process-wide cached genai client for the classifier.

    The classifier runs in ``before_agent_callback`` on the hot path, ahead of
    the first streamed token. Building a fresh ``genai.Client`` per request pays
    credential resolution + setup every turn, stacking latency that can tip a
    borderline request into an empty-at-200 timeout. The client is stateless and
    thread-safe to reuse, so build it once per container.
    """
    global _CLASSIFIER_CLIENT
    if _CLASSIFIER_CLIENT is None:
        _CLASSIFIER_CLIENT = genai.Client(
            vertexai=True, project=GCP_PROJECT_ID, location=CLASSIFIER_LOCATION
        )
    return _CLASSIFIER_CLIENT


async def classify_complexity(prompt: str) -> ComplexityResult:
    client = _classifier_client()
    response = await client.aio.models.generate_content(
        model=CLASSIFIER_MODEL,
        contents=CLASSIFIER_PROMPT_TEMPLATE.format(prompt=prompt),
        # AFC off: this runs on every router request, and genai's default-on
        # automatic function calling logged an INFO per call plus a WARNING per
        # worker process (docs/notes/genai-afc-warning.md). The classifier has no
        # tools, so the AFC branch was pure overhead.
        config=with_afc_disabled(
            GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
                # Thinking models (gemini-3.x) use output tokens for reasoning,
                # so we need extra headroom beyond the ~80 tokens of JSON output
                max_output_tokens=2048,
                temperature=0.0,
            )
        ),
    )
    # Thinking models may return None text if all tokens went to reasoning
    text = response.text
    if not text:
        return ComplexityResult(level="low", score=0.1, reason="classifier returned empty response")
    data = json.loads(text)
    score = max(0.0, min(1.0, float(data["score"])))
    return ComplexityResult(
        level=_score_to_level(score),
        score=score,
        reason=data.get("reason", ""),
    )
