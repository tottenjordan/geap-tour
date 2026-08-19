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

from typing import TYPE_CHECKING

from google.adk.models.base_llm import BaseLlm
from google.adk.models.registry import LLMRegistry
from pydantic import PrivateAttr

from src.config import resolve_model

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse


def _as_base_llm(model_id: str) -> BaseLlm:
    """Materialize a concrete ``BaseLlm`` for a tier model id.

    ``resolve_model`` returns a ready ``BaseLlm`` for Gemini-3 (native, global
    endpoint) and Claude (LiteLlm) — use it as-is so the endpoint wiring is
    preserved. For Gemini-2.x it returns a plain string (regional endpoint);
    materialize it exactly as ADK's ``canonical_model`` does.
    """
    resolved = resolve_model(model_id)
    if isinstance(resolved, BaseLlm):
        return resolved
    return LLMRegistry.new_llm(resolved)


class TierRoutingLlm(BaseLlm):
    """Dispatch each turn to the tier model named by ``llm_request.model``.

    Set once as the router agent's ``model``. Stateless => race-safe.
    """

    _resolved: dict[str, BaseLlm] = PrivateAttr(default_factory=dict)
    _default_model: str = PrivateAttr(default="")

    def __init__(
        self,
        tier_models: list[str],
        *,
        default_model: str,
        resolver: Callable[[str], BaseLlm] = _as_base_llm,
    ) -> None:
        # BaseLlm requires a ``model`` string; use the default tier so
        # ``canonical_model`` accepts the instance and billing/labels have a
        # sensible fallback before the callback selects a tier.
        super().__init__(model=default_model)
        self._default_model = default_model
        for model_id in tier_models:
            if model_id not in self._resolved:
                self._resolved[model_id] = resolver(model_id)

    def _select(self, requested: str | None) -> BaseLlm:
        """The underlying model for ``requested`` (falls back to the default)."""
        if requested and requested in self._resolved:
            return self._resolved[requested]
        return self._resolved[self._default_model]

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        """Forward the turn to the tier model chosen for this request."""
        underlying = self._select(llm_request.model)
        async for response in underlying.generate_content_async(llm_request, stream=stream):
            yield response
