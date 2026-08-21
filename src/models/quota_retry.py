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
from src.models.tool_call_ids import is_litellm_backed, restore_tool_call_ids

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

# Prefix of the last-resort reply when every attempt came back silent (no 429,
# no exception — just no content). Greppable for the same reason as the throttle
# prefix: an eval must see a labelled infra failure, not a bad answer.
EMPTY_RESPONSE_PREFIX = "The model returned an empty response"

_EMPTY_RESPONSE_TEXT = f"{EMPTY_RESPONSE_PREFIX} after retrying. Please try again in a moment."


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


def _has_visible_output(response: object) -> bool:
    """True if the caller would see *anything* from this response.

    Visible means a text part, a ``function_call`` (a normal tool hop — the turn
    is progressing even though no text has appeared yet), a ``function_response``,
    or an explicit ``error_code`` (a labelled failure is not silence). Everything
    else — no content, empty ``parts``, or parts carrying none of the above — is
    invisible, and a turn made **entirely** of those is an empty-at-200.

    Duck-typed rather than keyed to ``LlmResponse`` so a LiteLlm-wrapped Claude
    backbone and the streaming chunk shapes are both covered.
    """
    if getattr(response, "error_code", None):
        return True
    text = getattr(response, "text", None)
    if isinstance(text, str) and text:
        return True
    content = getattr(response, "content", None)
    parts = getattr(content, "parts", None) if content is not None else None
    for part in parts or []:
        if (
            getattr(part, "text", None)
            or getattr(part, "function_call", None)
            or getattr(part, "function_response", None)
        ):
            return True
    return False


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


def _empty_response(model: str) -> LlmResponse:
    """The last-resort reply for a turn that stayed silent through retries."""
    return LlmResponse(
        content=types.Content(role="model", parts=[types.Part(text=_EMPTY_RESPONSE_TEXT)]),
        error_code="EMPTY_RESPONSE",
        error_message=f"{model} produced no content on any attempt",
        turn_complete=True,
    )


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
    _retry_empty: bool = PrivateAttr(default=True)
    _sleep: Callable[[float], Awaitable[None]] = PrivateAttr()

    def __init__(
        self,
        inner: BaseLlm,
        *,
        retry_attempts: int = DEFAULT_QUOTA_RETRY_ATTEMPTS,
        retry_base_delay: float = DEFAULT_QUOTA_RETRY_BASE_DELAY,
        retry_empty: bool = True,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(model=inner.model)
        self._inner = inner
        self._retry_attempts = max(1, int(retry_attempts))
        self._retry_base_delay = float(retry_base_delay)
        self._retry_empty = bool(retry_empty)
        self._sleep = sleep

    @property
    def inner(self) -> BaseLlm:
        """The wrapped backbone (for tests and introspection)."""
        return self._inner

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        """Forward the turn, retrying a quota rejection **or a silent turn**.

        Two ways a turn becomes an empty-at-200, both handled here:

        * **Throttled** — the inner model raises a 429. Retried with exponential
          backoff; exhausted retries end in :func:`_throttled_response`.
        * **Silent** — the inner generator completes normally having produced no
          visible output at all (no exception, no 429). This is the residual
          empty-at-200 that survived the quota fix; it fell straight through to
          ``return``, so the caller got HTTP 200 and zero characters. Now retried
          on the same budget, ending in :func:`_empty_response`.

        A turn that has **already produced visible output is never retried**: the
        client holds that partial answer and a retry would duplicate it. Note the
        guard is *visible* output, not merely "yielded something" — a response
        carrying no content is invisible to the caller, so re-running after one is
        safe (it cannot duplicate anything the user can see).

        A ``function_call`` counts as visible, so a normal tool hop — which
        legitimately carries no text — is never mistaken for silence and never
        re-runs its tool.

        Wrapping also hides a LiteLlm backbone from ADK's ``isinstance`` check, so
        ADK strips the tool-call ids that Anthropic pairs results by; they are
        restored here, once per turn, before the first attempt.
        """
        if is_litellm_backed(self._inner):
            restore_tool_call_ids(llm_request)

        for attempt in range(self._retry_attempts):
            streamed = False
            try:
                async for response in self._inner.generate_content_async(
                    llm_request, stream=stream
                ):
                    streamed = streamed or _has_visible_output(response)
                    yield response
                if streamed or not self._retry_empty:
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
                continue

            # Fell through the try without an exception and without visible
            # output: a silent turn.
            if attempt == self._retry_attempts - 1:
                logger.error(
                    "Model %s returned no content on all %d attempts",
                    self.model,
                    self._retry_attempts,
                )
                yield _empty_response(self.model)
                return
            delay = self._retry_base_delay * (2**attempt)
            logger.warning(
                "Model %s returned an empty turn (attempt %d/%d), retrying in %.1fs",
                self.model,
                attempt + 1,
                self._retry_attempts,
                delay,
            )
            await self._sleep(delay)


def retrying_model(model_id: str, **kwargs) -> RetryingLlm:
    """``resolve_model(model_id)`` materialized into a ``BaseLlm``, then wrapped."""
    return RetryingLlm(as_base_llm(model_id), **kwargs)
