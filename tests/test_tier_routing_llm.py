"""Tests for the per-request tier-model dispatcher (no GCP)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.router.tier_routing_llm import TierRoutingLlm


class _FakeLlm:
    """Minimal BaseLlm stand-in that records the request and yields one chunk."""

    def __init__(self, tag: str, model: str | None = None):
        self.tag = tag
        self.model = model if model is not None else tag
        self.seen: list = []

    async def generate_content_async(self, llm_request, stream=False):
        self.seen.append((llm_request, stream))
        yield SimpleNamespace(text=f"resp-from-{self.tag}", partial=False)


def _dispatcher(models):
    """Build a dispatcher whose tiers resolve to tagged fakes."""
    fakes = {m: _FakeLlm(m) for m in models}
    disp = TierRoutingLlm(models, default_model=models[0], resolver=lambda m: fakes[m])
    return disp, fakes


async def _collect(agen):
    return [r async for r in agen]


@pytest.mark.asyncio
async def test_dispatch_selects_requested_tier():
    disp, fakes = _dispatcher(["lite-x", "flash-x", "opus-x"])
    req = SimpleNamespace(model="flash-x")
    out = await _collect(disp.generate_content_async(req))
    assert out[0].text == "resp-from-flash-x"
    assert len(fakes["flash-x"].seen) == 1
    assert not fakes["lite-x"].seen  # others untouched


@pytest.mark.asyncio
async def test_unknown_model_falls_back_to_default():
    disp, _fakes = _dispatcher(["lite-x", "flash-x"])
    req = SimpleNamespace(model="does-not-exist")
    out = await _collect(disp.generate_content_async(req))
    assert out[0].text == "resp-from-lite-x"  # default is first tier


@pytest.mark.asyncio
async def test_none_model_falls_back_to_default():
    disp, _ = _dispatcher(["lite-x", "flash-x"])
    req = SimpleNamespace(model=None)
    out = await _collect(disp.generate_content_async(req))
    assert out[0].text == "resp-from-lite-x"


@pytest.mark.asyncio
async def test_stream_flag_is_forwarded():
    disp, fakes = _dispatcher(["lite-x"])
    req = SimpleNamespace(model="lite-x")
    await _collect(disp.generate_content_async(req, stream=True))
    assert fakes["lite-x"].seen[0][1] is True


def test_is_base_llm_and_carries_default_model():
    from google.adk.models.base_llm import BaseLlm

    disp, _ = _dispatcher(["lite-x", "flash-x"])
    assert isinstance(disp, BaseLlm)
    assert disp.model == "lite-x"  # BaseLlm.model field == default tier


@pytest.mark.asyncio
async def test_request_model_rewritten_to_underlying_model():
    """The tier's *resolved* id reaches the backbone, not the bare tier key.

    ADK's ``LiteLlm.generate_content_async`` uses
    ``effective_model = llm_request.model or self.model``, so leaving the bare
    key on the request discards ``resolve_model``'s ``vertex_ai/`` prefix and
    LiteLLM routes Claude to provider=anthropic (Missing Anthropic API Key).
    """
    fake = _FakeLlm("claude-x", model="vertex_ai/claude-x")
    disp = TierRoutingLlm(["claude-x"], default_model="claude-x", resolver=lambda _m: fake)
    req = SimpleNamespace(model="claude-x")

    await _collect(disp.generate_content_async(req))

    assert req.model == "vertex_ai/claude-x"
    assert fake.seen[0][0].model == "vertex_ai/claude-x"


@pytest.mark.asyncio
async def test_fallback_rewrites_request_model_to_default():
    """An unknown tier runs on the default, so the request must name the default."""
    disp, fakes = _dispatcher(["lite-x", "flash-x"])
    req = SimpleNamespace(model="does-not-exist")

    await _collect(disp.generate_content_async(req))

    assert req.model == "lite-x"
    assert fakes["lite-x"].seen[0][0].model == "lite-x"


def test_claude_tier_resolves_with_vertex_prefix():
    """Regression guard on the resolver itself (no network)."""
    from src.router.tier_routing_llm import _as_base_llm

    assert _as_base_llm("claude-sonnet-4-6").model == "vertex_ai/claude-sonnet-4-6"


def test_resolver_called_once_per_unique_model():
    calls: list[str] = []

    def counting_resolver(m):
        calls.append(m)
        return _FakeLlm(m)

    TierRoutingLlm(["a", "b", "a"], default_model="a", resolver=counting_resolver)
    assert sorted(calls) == ["a", "b"]  # duplicate 'a' resolved once
