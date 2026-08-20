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

    disp = TierRoutingLlm(["a", "b", "a"], default_model="a", resolver=counting_resolver)
    disp._select("a")
    disp._select("b")
    disp._select("a")  # duplicate 'a' resolved once, then cached
    assert sorted(calls) == ["a", "b"]


def test_tier_models_are_resolved_lazily():
    """Constructing the dispatcher must NOT resolve every tier backbone.

    Resolving the Claude tiers imports ``litellm``, ~140MB resident — paid by
    EVERY router worker at import even when it only ever serves Gemini-tier
    traffic. Measured: router container 308MB vs coordinator 168MB, the entire
    gap being litellm. Resolve on first use instead.
    """
    calls: list[str] = []

    def counting_resolver(m):
        calls.append(m)
        return _FakeLlm(m)

    TierRoutingLlm(["lite-x", "claude-x"], default_model="lite-x", resolver=counting_resolver)
    assert calls == []  # nothing resolved at construction


@pytest.mark.asyncio
async def test_only_the_requested_tier_is_resolved():
    """Serving a lite turn must not resolve (and so must not import) Claude."""
    calls: list[str] = []

    def counting_resolver(m):
        calls.append(m)
        return _FakeLlm(m)

    disp = TierRoutingLlm(
        ["lite-x", "claude-x"], default_model="lite-x", resolver=counting_resolver
    )
    await _collect(disp.generate_content_async(SimpleNamespace(model="lite-x")))
    assert calls == ["lite-x"]


class _QuotaError(Exception):
    """Stand-in for ``google.genai.errors.ClientError`` on a 429."""

    def __init__(self, message="RESOURCE_EXHAUSTED: quota exceeded", code=429):
        super().__init__(message)
        self.code = code


class _FlakyLlm:
    """Raises ``exc`` for the first ``n_failures`` calls, then succeeds."""

    def __init__(self, tag="lite-x", n_failures=1, exc=None, fail_after_yield=False):
        self.model = tag
        self.tag = tag
        self.calls = 0
        self._n_failures = n_failures
        self._exc = exc or _QuotaError()
        self._fail_after_yield = fail_after_yield

    async def generate_content_async(self, llm_request, stream=False):
        self.calls += 1
        if self.calls <= self._n_failures:
            if self._fail_after_yield:
                yield SimpleNamespace(text="partial", partial=True)
            raise self._exc
        yield SimpleNamespace(text=f"resp-from-{self.tag}", partial=False)


def _retrying(llm, **kwargs):
    """Dispatcher over a single tier backed by ``llm``, with instant sleeps."""
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    disp = TierRoutingLlm(
        [llm.model],
        default_model=llm.model,
        resolver=lambda _m: llm,
        sleep=fake_sleep,
        **kwargs,
    )
    return disp, slept


def test_quota_error_detection():
    """Only genuine RESOURCE_EXHAUSTED/429 failures are retryable."""
    from src.router.tier_routing_llm import _is_quota_error

    assert _is_quota_error(_QuotaError()) is True
    assert _is_quota_error(_QuotaError("429 Too Many Requests", code=429)) is True
    assert _is_quota_error(Exception("Quota exceeded for GenerateContent")) is True
    assert _is_quota_error(ValueError("bad request")) is False
    assert _is_quota_error(_QuotaError("permission denied", code=403)) is False


@pytest.mark.asyncio
async def test_retries_a_throttled_tier_call_and_succeeds():
    """A 429 on the tier model must not become an empty stream.

    Vertex ``GenerateContent`` throttles the router under load (215 HTTP 429s in
    two hours, all attributed to the router engine); google-genai raises
    ``ClientError``, ADK yields nothing, and the caller sees an empty-at-200
    stream. Retrying with backoff turns the throttle into a slower answer.
    """
    llm = _FlakyLlm(n_failures=2)
    disp, slept = _retrying(llm, retry_attempts=3, retry_base_delay=2.0)

    out = await _collect(disp.generate_content_async(SimpleNamespace(model="lite-x")))

    assert out[0].text == "resp-from-lite-x"
    assert llm.calls == 3
    assert slept == [2.0, 4.0]  # exponential backoff


@pytest.mark.asyncio
async def test_exhausted_retries_yield_a_visible_throttle_message():
    """Never return silence: the last resort is an explicit, labelled error."""
    from src.router.tier_routing_llm import THROTTLED_RESPONSE_PREFIX

    llm = _FlakyLlm(n_failures=99)
    disp, slept = _retrying(llm, retry_attempts=2, retry_base_delay=1.0)

    out = await _collect(disp.generate_content_async(SimpleNamespace(model="lite-x")))

    assert llm.calls == 2
    # Two attempts => exactly one backoff between them, and none after the last.
    assert slept == [1.0]
    assert len(out) == 1
    text = "".join(p.text or "" for p in out[0].content.parts)
    assert text.startswith(THROTTLED_RESPONSE_PREFIX)
    assert out[0].error_code == "RESOURCE_EXHAUSTED"


@pytest.mark.asyncio
async def test_non_quota_errors_are_not_retried():
    llm = _FlakyLlm(n_failures=1, exc=ValueError("malformed request"))
    disp, _ = _retrying(llm, retry_attempts=3)

    with pytest.raises(ValueError, match="malformed request"):
        await _collect(disp.generate_content_async(SimpleNamespace(model="lite-x")))
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_a_partially_streamed_turn_is_never_retried():
    """Retrying after chunks reached the client would duplicate output."""
    llm = _FlakyLlm(n_failures=1, fail_after_yield=True)
    disp, _ = _retrying(llm, retry_attempts=3)

    with pytest.raises(_QuotaError):
        await _collect(disp.generate_content_async(SimpleNamespace(model="lite-x")))
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_a_healthy_tier_call_never_sleeps():
    llm = _FlakyLlm(n_failures=0)
    disp, slept = _retrying(llm, retry_attempts=3)

    out = await _collect(disp.generate_content_async(SimpleNamespace(model="lite-x")))

    assert out[0].text == "resp-from-lite-x"
    assert llm.calls == 1
    assert slept == []
