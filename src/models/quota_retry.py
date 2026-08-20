"""Turn a Vertex quota rejection into a slower answer — or a labelled error —
but never into silence.

**Why this exists.** Vertex rejects ``GenerateContent`` with HTTP 429
``RESOURCE_EXHAUSTED`` under load. google-genai raises that as a ``ClientError``,
ADK's model layer yields nothing, and the caller gets an **empty-at-200 stream**:
HTTP 200, events, zero characters of text. That is the worst possible answer —
indistinguishable from a bad model response, and invisible to any eval scoring
response text.

It is not hypothetical. The deployed 5-tier router answered ~40% of turns with
nothing at all, and Cloud Monitoring attributed **215 HTTP 429s in two hours** to
that one engine (``docs/notes/router-empty-responses-quota.md``). The retry
originally lived inside the router's tier dispatcher; it lives here so the
coordinator — and any future agent — gets the same protection without importing
``src.router``.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.models.registry import LLMRegistry
from google.genai import types
from pydantic import PrivateAttr

from src.config import resolve_model

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from google.adk.models.llm_request import LlmRequest

logger = logging.getLogger(__name__)

# Retry budget for a throttled call. Three attempts at 2s/4s covers the bursty
# part of a per-minute quota window without stretching a turn past the demo's
# patience.
DEFAULT_QUOTA_RETRY_ATTEMPTS = 3
DEFAULT_QUOTA_RETRY_BASE_DELAY = 2.0

# Prefix of the last-resort reply when every retry is throttled. Greppable on
# purpose: an eval scoring this text should see a labelled infra failure, not a
# low-quality answer.
THROTTLED_RESPONSE_PREFIX = "The model is temporarily rate-limited (RESOURCE_EXHAUSTED)"

_THROTTLED_RESPONSE_TEXT = (
    f"{THROTTLED_RESPONSE_PREFIX} and the request could not be completed after "
    "retrying. Please try again in a moment."
)


def _is_quota_error(exc: BaseException) -> bool:
    """True for a Vertex quota rejection (HTTP 429 / RESOURCE_EXHAUSTED).

    Duck-typed rather than keyed to ``google.genai.errors.ClientError`` so a
    LiteLlm-wrapped Claude backbone — which raises its own exception type for the
    same condition — is covered by the same retry.
    """
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code == 429:
        return True
    if code is not None and code != 429:
        return False
    text = str(exc).upper()
    return "RESOURCE_EXHAUSTED" in text or "QUOTA EXCEEDED" in text


def _quiet_litellm() -> None:
    """Silence LiteLLM's debug banner — but only if litellm is already loaded.

    Deliberately reads ``sys.modules`` instead of importing: a plain ``import
    litellm`` here would add ~140MB resident to every worker that never serves a
    Claude backbone. By the time a LiteLlm-backed model has been resolved,
    litellm IS loaded, and this still runs before its first call.
    """
    litellm = sys.modules.get("litellm")
    if litellm is not None:
        litellm.suppress_debug_info = True  # ty: ignore[unresolved-attribute]


def as_base_llm(model_id: str) -> BaseLlm:
    """Materialize a concrete ``BaseLlm`` for a model id.

    ``resolve_model`` returns a ready ``BaseLlm`` for Gemini-3 (native, global
    endpoint) and Claude (LiteLlm) — use it as-is so the endpoint wiring is
    preserved. For Gemini-2.x it returns a plain string (regional endpoint);
    materialize it exactly as ADK's ``canonical_model`` does.
    """
    resolved = resolve_model(model_id)
    _quiet_litellm()
    if isinstance(resolved, BaseLlm):
        return resolved
    return LLMRegistry.new_llm(resolved)


def _throttled_response(model: str) -> LlmResponse:
    """The last-resort reply for a turn that stayed throttled through retries.

    Carries the text so the user sees *something* instead of an empty stream,
    and ``error_code``/``error_message`` so programmatic consumers (evals,
    monitors) can tell an infra throttle from a bad answer.
    """
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=_THROTTLED_RESPONSE_TEXT)]),
        error_code="RESOURCE_EXHAUSTED",
        error_message=f"{model} exhausted its quota retries",
        turn_complete=True,
    )


class RetryingLlm(BaseLlm):
    """Wrap a ``BaseLlm`` so a Vertex 429 becomes a slower answer, never silence.

    Transparent otherwise: ``.model`` reports the wrapped backbone's id (which
    ``LlmAgent``, billing and resource labels all read), and every response is
    forwarded through unchanged.
    """

    _inner: BaseLlm = PrivateAttr()
    _retry_attempts: int = PrivateAttr(default=DEFAULT_QUOTA_RETRY_ATTEMPTS)
    _retry_base_delay: float = PrivateAttr(default=DEFAULT_QUOTA_RETRY_BASE_DELAY)
    _sleep: Callable[[float], Awaitable[None]] = PrivateAttr()

    def __init__(
        self,
        inner: BaseLlm,
        *,
        retry_attempts: int = DEFAULT_QUOTA_RETRY_ATTEMPTS,
        retry_base_delay: float = DEFAULT_QUOTA_RETRY_BASE_DELAY,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(model=inner.model)
        self._inner = inner
        self._retry_attempts = max(1, int(retry_attempts))
        self._retry_base_delay = float(retry_base_delay)
        self._sleep = sleep

    @property
    def inner(self) -> BaseLlm:
        """The wrapped backbone (for tests and introspection)."""
        return self._inner

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        """Forward the turn, retrying a quota rejection with exponential backoff.

        A turn that has **already streamed a chunk is never retried**: the client
        holds that partial output, so a retry would duplicate it. A turn
        throttled through every attempt ends in an explicit, labelled message
        rather than an empty stream.
        """
        for attempt in range(self._retry_attempts):
            streamed = False
            try:
                async for response in self._inner.generate_content_async(
                    llm_request, stream=stream
                ):
                    streamed = True
                    yield response
                return
            except Exception as exc:
                if streamed or not _is_quota_error(exc):
                    raise
                if attempt == self._retry_attempts - 1:
                    logger.error(
                        "Model %s throttled on all %d attempts: %s",
                        self.model,
                        self._retry_attempts,
                        exc,
                    )
                    yield _throttled_response(self.model)
                    return
                delay = self._retry_base_delay * (2**attempt)
                logger.warning(
                    "Model %s throttled (attempt %d/%d), retrying in %.1fs: %s",
                    self.model,
                    attempt + 1,
                    self._retry_attempts,
                    delay,
                    exc,
                )
                await self._sleep(delay)


def retrying_model(model_id: str, **kwargs) -> RetryingLlm:
    """``resolve_model(model_id)`` materialized into a ``BaseLlm``, then wrapped."""
    return RetryingLlm(as_base_llm(model_id), **kwargs)
