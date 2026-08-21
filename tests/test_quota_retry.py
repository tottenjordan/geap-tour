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


class _SilentLlm:
    """Yields nothing (or contentless responses) for the first ``n_silent`` calls.

    Models the residual **empty-at-200** turn: no exception, no 429, just a
    generator that completes without ever producing visible output.
    """

    def __init__(self, tag="lite-x", n_silent=1, yield_contentless=False):
        self.model = tag
        self.tag = tag
        self.calls = 0
        self._n_silent = n_silent
        self._yield_contentless = yield_contentless

    async def generate_content_async(self, llm_request, stream=False):
        self.calls += 1
        self.last_stream = stream
        if self.calls <= self._n_silent:
            if self._yield_contentless:
                yield SimpleNamespace(content=None, text=None, error_code=None)
            return
        yield SimpleNamespace(text=f"resp-from-{self.tag}", partial=False)


class _ToolCallLlm:
    """Yields a single ``function_call`` part and no text — a normal tool hop."""

    def __init__(self, tag="lite-x"):
        self.model = tag
        self.calls = 0

    async def generate_content_async(self, llm_request, stream=False):
        self.calls += 1
        part = SimpleNamespace(
            text=None, function_call=SimpleNamespace(name="search_flights"), function_response=None
        )
        yield SimpleNamespace(content=SimpleNamespace(parts=[part]), text=None, error_code=None)


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


class TestSilentTurnRetry:
    """A turn that yields *nothing* is the residual empty-at-200 (no 429 involved).

    The quota retry only ever fired on an exception, so a generator that
    completed without producing visible output fell straight through to
    ``return`` — silence, unretried. Measured live on router
    ``6134089059699523584``: 23/24 turns full, 1 turn with both MCP calls
    executed and 0 characters of text (docs/notes/router-empty-stream-retry.md).
    """

    @pytest.mark.asyncio
    async def test_a_silent_turn_is_retried_and_recovers(self):
        llm = _SilentLlm(n_silent=1)
        wrapped, slept = _retrying(llm, retry_attempts=3, retry_base_delay=2.0)

        out = await _collect(wrapped.generate_content_async(SimpleNamespace(model="lite-x")))

        assert [o.text for o in out] == ["resp-from-lite-x"]
        assert llm.calls == 2
        assert slept == [2.0]

    @pytest.mark.asyncio
    async def test_a_turn_yielding_only_contentless_responses_is_retried(self):
        """Yielding an event with no parts is still silence to the user."""
        llm = _SilentLlm(n_silent=1, yield_contentless=True)
        wrapped, _ = _retrying(llm, retry_attempts=3)

        out = await _collect(wrapped.generate_content_async(SimpleNamespace(model="lite-x")))

        assert llm.calls == 2
        assert any(getattr(o, "text", None) == "resp-from-lite-x" for o in out)

    @pytest.mark.asyncio
    async def test_exhausted_silent_retries_yield_a_labelled_message(self):
        """Never return silence — the last resort is greppable and labelled."""
        from src.models.quota_retry import EMPTY_RESPONSE_PREFIX

        llm = _SilentLlm(n_silent=99)
        wrapped, slept = _retrying(llm, retry_attempts=2, retry_base_delay=1.0)

        out = await _collect(wrapped.generate_content_async(SimpleNamespace(model="lite-x")))

        assert llm.calls == 2
        assert slept == [1.0]
        assert len(out) == 1
        text = "".join(p.text or "" for p in out[0].content.parts)
        assert text.startswith(EMPTY_RESPONSE_PREFIX)
        assert out[0].error_code == "EMPTY_RESPONSE"

    @pytest.mark.asyncio
    async def test_a_tool_call_hop_is_not_treated_as_silent(self):
        """A ``function_call`` with no text is a NORMAL hop — retrying it would
        re-run the tool and double every tool-using turn's latency."""
        llm = _ToolCallLlm()
        wrapped, slept = _retrying(llm, retry_attempts=3)

        out = await _collect(wrapped.generate_content_async(SimpleNamespace(model="lite-x")))

        assert llm.calls == 1
        assert slept == []
        assert len(out) == 1

    @pytest.mark.asyncio
    async def test_retry_empty_can_be_disabled(self):
        llm = _SilentLlm(n_silent=99)
        wrapped, slept = _retrying(llm, retry_attempts=3, retry_empty=False)

        out = await _collect(wrapped.generate_content_async(SimpleNamespace(model="lite-x")))

        assert llm.calls == 1
        assert slept == []
        assert out == []

    def test_visible_output_predicate(self):
        from src.models.quota_retry import _has_visible_output

        empty_part = SimpleNamespace(text=None, function_call=None, function_response=None)
        text_part = SimpleNamespace(text="hi", function_call=None, function_response=None)
        resp_part = SimpleNamespace(
            text=None, function_call=None, function_response=SimpleNamespace(name="t")
        )

        def _r(parts=None, **kw):
            content = SimpleNamespace(parts=parts) if parts is not None else None
            return SimpleNamespace(content=content, text=None, error_code=None, **kw)

        assert _has_visible_output(_r()) is False
        assert _has_visible_output(_r([])) is False
        assert _has_visible_output(_r([empty_part])) is False
        assert _has_visible_output(_r([text_part])) is True
        assert _has_visible_output(_r([resp_part])) is True
        # An explicitly labelled error is not silence — the caller sees it.
        assert _has_visible_output(SimpleNamespace(content=None, error_code="X")) is True


class TestToolCallIdRestoration:
    """A wrapped LiteLlm loses ADK's tool-call ids — see
    ``src/models/tool_call_ids.py`` and docs/notes/router-empty-stream-retry.md."""

    def _request_with_a_tool_result(self):
        from google.genai import types

        return SimpleNamespace(
            model="vertex_ai/claude-x",
            contents=[
                types.Content(
                    role="model",
                    parts=[
                        types.Part(function_call=types.FunctionCall(name="f", args={}, id=None))
                    ],
                ),
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(name="f", response={}, id=None)
                        )
                    ],
                ),
            ],
        )

    @pytest.mark.asyncio
    async def test_ids_are_restored_for_a_litellm_backbone(self, monkeypatch):
        import src.models.quota_retry as qr

        monkeypatch.setattr(qr, "is_litellm_backed", lambda _m: True)
        wrapped, _ = _retrying(_FlakyLlm(n_failures=0))
        req = self._request_with_a_tool_result()

        await _collect(wrapped.generate_content_async(req))

        call_id = req.contents[0].parts[0].function_call.id
        assert call_id
        assert req.contents[1].parts[0].function_response.id == call_id

    @pytest.mark.asyncio
    async def test_a_gemini_backbone_is_left_untouched(self, monkeypatch):
        """Gemini rejects ids it never issued — restoring them would break it."""
        import src.models.quota_retry as qr

        monkeypatch.setattr(qr, "is_litellm_backed", lambda _m: False)
        wrapped, _ = _retrying(_FlakyLlm(n_failures=0))
        req = self._request_with_a_tool_result()

        await _collect(wrapped.generate_content_async(req))

        assert req.contents[0].parts[0].function_call.id is None

    @pytest.mark.asyncio
    async def test_ids_are_restored_once_not_per_retry(self, monkeypatch):
        """A retry must not renumber ids the provider has already seen."""
        import src.models.quota_retry as qr

        monkeypatch.setattr(qr, "is_litellm_backed", lambda _m: True)
        wrapped, _ = _retrying(_FlakyLlm(n_failures=1), retry_attempts=3)
        req = self._request_with_a_tool_result()

        await _collect(wrapped.generate_content_async(req))

        call_id = req.contents[0].parts[0].function_call.id
        assert req.contents[1].parts[0].function_response.id == call_id


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
