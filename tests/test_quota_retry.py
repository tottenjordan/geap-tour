"""Tests for the shared quota-retry ``BaseLlm`` wrapper (no GCP)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.models.quota_retry import RetryingLlm


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
        self.last_stream = stream
        if self.calls <= self._n_failures:
            if self._fail_after_yield:
                yield SimpleNamespace(text="partial", partial=True)
            raise self._exc
        yield SimpleNamespace(text=f"resp-from-{self.tag}", partial=False)


def _retrying(llm, **kwargs):
    """Wrap ``llm`` with instant (recorded) sleeps."""
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    return RetryingLlm(llm, sleep=fake_sleep, **kwargs), slept


async def _collect(agen):
    return [r async for r in agen]


def test_is_a_base_llm_and_reports_the_wrapped_model_id():
    """``LlmAgent`` and the billing/label surface both read ``.model``."""
    from google.adk.models.base_llm import BaseLlm

    wrapped, _ = _retrying(_FlakyLlm(tag="vertex_ai/claude-x"))
    assert isinstance(wrapped, BaseLlm)
    assert wrapped.model == "vertex_ai/claude-x"


def test_quota_error_detection():
    """Only genuine RESOURCE_EXHAUSTED/429 failures are retryable."""
    from src.models.quota_retry import _is_quota_error

    assert _is_quota_error(_QuotaError()) is True
    assert _is_quota_error(_QuotaError("429 Too Many Requests", code=429)) is True
    assert _is_quota_error(Exception("Quota exceeded for GenerateContent")) is True
    assert _is_quota_error(ValueError("bad request")) is False
    assert _is_quota_error(_QuotaError("permission denied", code=403)) is False


@pytest.mark.asyncio
async def test_retries_a_throttled_call_and_succeeds():
    """A 429 must not become an empty stream.

    Vertex ``GenerateContent`` throttled the router under load (215 HTTP 429s in
    two hours, all attributed to one engine); google-genai raises
    ``ClientError``, ADK yields nothing, and the caller sees an empty-at-200
    stream. Retrying with backoff turns the throttle into a slower answer.
    """
    llm = _FlakyLlm(n_failures=2)
    wrapped, slept = _retrying(llm, retry_attempts=3, retry_base_delay=2.0)

    out = await _collect(wrapped.generate_content_async(SimpleNamespace(model="lite-x")))

    assert out[0].text == "resp-from-lite-x"
    assert llm.calls == 3
    assert slept == [2.0, 4.0]  # exponential backoff


@pytest.mark.asyncio
async def test_exhausted_retries_yield_a_visible_throttle_message():
    """Never return silence: the last resort is an explicit, labelled error."""
    from src.models.quota_retry import THROTTLED_RESPONSE_PREFIX

    llm = _FlakyLlm(n_failures=99)
    wrapped, slept = _retrying(llm, retry_attempts=2, retry_base_delay=1.0)

    out = await _collect(wrapped.generate_content_async(SimpleNamespace(model="lite-x")))

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
    wrapped, _ = _retrying(llm, retry_attempts=3)

    with pytest.raises(ValueError, match="malformed request"):
        await _collect(wrapped.generate_content_async(SimpleNamespace(model="lite-x")))
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_a_partially_streamed_turn_is_never_retried():
    """Retrying after chunks reached the client would duplicate output."""
    llm = _FlakyLlm(n_failures=1, fail_after_yield=True)
    wrapped, _ = _retrying(llm, retry_attempts=3)

    with pytest.raises(_QuotaError):
        await _collect(wrapped.generate_content_async(SimpleNamespace(model="lite-x")))
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_a_healthy_call_never_sleeps():
    llm = _FlakyLlm(n_failures=0)
    wrapped, slept = _retrying(llm, retry_attempts=3)

    out = await _collect(wrapped.generate_content_async(SimpleNamespace(model="lite-x")))

    assert out[0].text == "resp-from-lite-x"
    assert llm.calls == 1
    assert slept == []


@pytest.mark.asyncio
async def test_stream_flag_is_forwarded():
    llm = _FlakyLlm(n_failures=0)
    wrapped, _ = _retrying(llm)

    await _collect(wrapped.generate_content_async(SimpleNamespace(model="lite-x"), stream=True))

    assert llm.last_stream is True


def test_retrying_model_wraps_a_resolved_backbone():
    """``retrying_model`` is the coordinator's one-liner: resolve, then wrap."""
    from src.models.quota_retry import retrying_model

    wrapped = retrying_model("gemini-2.5-flash")
    assert isinstance(wrapped, RetryingLlm)
    assert wrapped.model == "gemini-2.5-flash"


def test_retrying_model_preserves_the_claude_vertex_prefix():
    """Regression guard: LiteLlm must still be told to route through Vertex."""
    from src.models.quota_retry import retrying_model

    assert retrying_model("claude-sonnet-4-6").model == "vertex_ai/claude-sonnet-4-6"
