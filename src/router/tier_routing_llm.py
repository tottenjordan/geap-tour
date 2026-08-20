"""A stateless ``BaseLlm`` dispatcher that runs each turn on the tier model
selected for the current request.

**Why this exists.** On the deployed Agent Engine runtime, only the *root*
agent's own output streams back reliably. Delegation — whether via
``transfer_to_agent`` (``sub_agents``) or a nested ``AgentTool`` whose sub-agent
makes MCP calls — does **not** stream through the managed runtime (measured
empirically at ~0/8 full completions; the transferred specialist's turn never
emits, see ``docs/notes/router-transfer-streaming.md``). The coordinator hit the
same wall and fixed it by holding its MCP toolsets *directly* on the root agent
(``src/agents/coordinator_agent.py``).

So the router can no longer be five sub-agents behind a transferring root. It is
now **one** root agent that holds the MCP toolsets directly and varies only its
*model* per complexity tier. Mutating the shared ``agent.model`` per request
would race across concurrent invocations, so instead this dispatcher is set once
as ``agent.model`` and, per request, reads the chosen model id from
``llm_request.model`` (written by ``select_tier_model_callback`` from the
classifier's verdict) and forwards ``generate_content_async`` to that tier's
pre-resolved ``BaseLlm``. The dispatcher holds no per-request state, so it is
race-safe.

The underlying tier models are built through :func:`src.config.resolve_model`, so
each tier keeps its correct endpoint wiring: Gemini-3 native on the global
endpoint, Claude via LiteLlm, Gemini-2.x on the regional endpoint (resolve_model
returns a plain string for 2.x, which we materialize the same way ADK's
``canonical_model`` does).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from google.adk.models.base_llm import BaseLlm
from pydantic import PrivateAttr

from src.models.quota_retry import (
    DEFAULT_QUOTA_RETRY_ATTEMPTS,
    DEFAULT_QUOTA_RETRY_BASE_DELAY,
    THROTTLED_RESPONSE_PREFIX,
    RetryingLlm,
    _is_quota_error,
    as_base_llm,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse

# Re-exported for callers that grew up importing them from here (the quota retry
# itself now lives in src/models/quota_retry.py so the coordinator can share it).
__all__ = [
    "DEFAULT_QUOTA_RETRY_ATTEMPTS",
    "DEFAULT_QUOTA_RETRY_BASE_DELAY",
    "THROTTLED_RESPONSE_PREFIX",
    "TierRoutingLlm",
    "_as_base_llm",
    "_is_quota_error",
]

_as_base_llm = as_base_llm


class TierRoutingLlm(BaseLlm):
    """Dispatch each turn to the tier model named by ``llm_request.model``.

    Set once as the router agent's ``model``. Stateless => race-safe.
    """

    _resolved: dict[str, BaseLlm] = PrivateAttr(default_factory=dict)
    _tier_models: tuple[str, ...] = PrivateAttr(default=())
    _resolver: Callable[[str], BaseLlm] = PrivateAttr()
    _default_model: str = PrivateAttr(default="")
    _retry_attempts: int = PrivateAttr(default=DEFAULT_QUOTA_RETRY_ATTEMPTS)
    _retry_base_delay: float = PrivateAttr(default=DEFAULT_QUOTA_RETRY_BASE_DELAY)
    _sleep: Callable[[float], Awaitable[None]] = PrivateAttr()

    def __init__(
        self,
        tier_models: list[str],
        *,
        default_model: str,
        resolver: Callable[[str], BaseLlm] = _as_base_llm,
        retry_attempts: int = DEFAULT_QUOTA_RETRY_ATTEMPTS,
        retry_base_delay: float = DEFAULT_QUOTA_RETRY_BASE_DELAY,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        # BaseLlm requires a ``model`` string; use the default tier so
        # ``canonical_model`` accepts the instance and billing/labels have a
        # sensible fallback before the callback selects a tier.
        super().__init__(model=default_model)
        self._default_model = default_model
        # Record the tier ids only — do NOT resolve them yet. Resolving the
        # Claude tiers constructs a LiteLlm, which imports litellm (~140MB
        # resident) into EVERY router worker at import time, even one that only
        # ever serves Gemini-tier traffic. Measured: router 308MB vs coordinator
        # 168MB, the whole gap being litellm. Resolving on first use keeps a
        # Gemini-only worker at coordinator-parity footprint.
        self._tier_models = tuple(dict.fromkeys(tier_models))
        self._resolver = resolver
        self._retry_attempts = max(1, int(retry_attempts))
        self._retry_base_delay = float(retry_base_delay)
        self._sleep = sleep

    def _select(self, requested: str | None) -> BaseLlm:
        """The underlying model for ``requested`` (falls back to the default).

        Resolves (and caches) the tier's backbone on first use, wrapped in
        :class:`~src.models.quota_retry.RetryingLlm` so a Vertex 429 on this tier
        becomes a slower answer instead of an empty-at-200 stream. Cached, so the
        wrapper is built once per tier.
        """
        key = requested if requested in self._tier_models else self._default_model
        if key not in self._resolved:
            self._resolved[key] = RetryingLlm(
                self._resolver(key),
                retry_attempts=self._retry_attempts,
                retry_base_delay=self._retry_base_delay,
                sleep=self._sleep,
            )
        return self._resolved[key]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        """Forward the turn to the tier model chosen for this request.

        Rewrites ``llm_request.model`` to the *resolved* backbone id before
        forwarding. ADK's ``LiteLlm.generate_content_async`` takes
        ``effective_model = llm_request.model or self.model``, i.e. the request
        wins over the instance — so leaving the bare tier key on the request
        would strip the ``vertex_ai/`` prefix ``resolve_model`` added, and
        LiteLLM would route Claude to provider ``anthropic`` (failing with
        "Missing Anthropic API Key" instead of reaching Vertex). Rewriting also
        keeps the request honest when ``_select`` falls back to the default.

        The quota retry lives in the ``RetryingLlm`` that ``_select`` returns, so
        a throttled tier call still ends in a slower answer or a labelled error —
        never an empty stream.
        """
        underlying = self._select(llm_request.model)
        llm_request.model = underlying.model
        async for response in underlying.generate_content_async(llm_request, stream=stream):
            yield response
