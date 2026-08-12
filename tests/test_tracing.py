"""Offline tests for agent-side OpenTelemetry spans.

Uses an in-memory span exporter to actually capture spans, so the attribute
wiring (router routing decision + no-op safety) is verified without a
collector or live GCP.
"""

import types

import pytest
from google.genai.types import Content, Part
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.util._once import Once

from src.observability.tracing import get_tracer, set_span_attributes, traced


@pytest.fixture
def span_exporter():
    """Install a fresh in-memory TracerProvider, then restore the previous one.

    Restoring lets the no-op-safety test run with the default (no real
    provider) tracer state that the rest of the suite relies on.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    saved_provider = trace._TRACER_PROVIDER
    saved_once = trace._TRACER_PROVIDER_SET_ONCE
    # Reset the "set once" guard so a fresh provider can be installed.
    trace._TRACER_PROVIDER_SET_ONCE = Once()
    trace._TRACER_PROVIDER = None
    trace.set_tracer_provider(provider)
    try:
        yield exporter
    finally:
        trace._TRACER_PROVIDER = saved_provider
        trace._TRACER_PROVIDER_SET_ONCE = saved_once


def _spans_by_name(exporter):
    return {s.name: s for s in exporter.get_finished_spans()}


class TestTracedContextManager:
    def test_produces_named_span_with_attributes(self, span_exporter):
        with traced("x", foo="bar"):
            pass
        spans = _spans_by_name(span_exporter)
        assert "x" in spans
        assert spans["x"].attributes["foo"] == "bar"

    def test_skips_none_attributes(self, span_exporter):
        with traced("x", foo="bar", empty=None):
            pass
        span = _spans_by_name(span_exporter)["x"]
        assert "empty" not in span.attributes
        assert span.attributes["foo"] == "bar"

    def test_records_exception_and_reraises(self, span_exporter):
        with pytest.raises(ValueError, match="boom"), traced("x"):
            raise ValueError("boom")
        span = _spans_by_name(span_exporter)["x"]
        assert span.status.status_code.name == "ERROR"
        event_names = [e.name for e in span.events]
        assert "exception" in event_names


class TestSetSpanAttributes:
    def test_sets_on_active_span(self, span_exporter):
        tracer = get_tracer()
        with tracer.start_as_current_span("y"):
            set_span_attributes(a=1, b="two", skip=None)
        span = _spans_by_name(span_exporter)["y"]
        assert span.attributes["a"] == 1
        assert span.attributes["b"] == "two"
        assert "skip" not in span.attributes

    def test_no_active_span_is_noop(self, span_exporter):
        # No active span: must not raise and must not create one.
        set_span_attributes(a=1)
        assert span_exporter.get_finished_spans() == ()


class TestRouterRouteSpan:
    def test_routing_callback_emits_router_route_span(self, span_exporter, monkeypatch):
        import src.router.agents as agents
        from src.router.complexity import ComplexityResult

        async def fake_classify(_prompt):
            return ComplexityResult(level="high", score=0.97, reason="multi-step")

        monkeypatch.setattr(agents, "classify_complexity", fake_classify)

        ctx = types.SimpleNamespace(
            user_content=Content(parts=[Part(text="plan a multi-city trip")]),
            state={},
        )

        import asyncio

        asyncio.run(agents.complexity_router_callback(callback_context=ctx))

        span = _spans_by_name(span_exporter)["router.route"]
        assert span.attributes["complexity.score"] == pytest.approx(0.97)
        assert span.attributes["routing.tier"] == "opus"
        assert span.attributes["model.id"]  # non-empty resolved model id
        # Boundary values are recorded so a trace shows what the score was
        # measured against.
        for key in (
            "boundaries.low",
            "boundaries.medium_split",
            "boundaries.high",
            "boundaries.high_split",
        ):
            assert key in span.attributes
        # Routing behavior is unchanged: state is still populated.
        assert ctx.state["model_tier"] == "opus"
        assert ctx.state["complexity_score"] == pytest.approx(0.97)

    def test_tier_to_model_helper(self):
        from src.config import LITE_MODEL, OPUS_MODEL
        from src.router.complexity import tier_to_model

        assert tier_to_model("lite") == LITE_MODEL
        assert tier_to_model("opus") == OPUS_MODEL
        assert tier_to_model("unknown") == LITE_MODEL


class TestNoProviderSafety:
    def test_get_tracer_returns_usable_tracer(self):
        tracer = get_tracer()
        assert tracer is not None
        # Usable even with the default (no real provider) tracer state.
        with tracer.start_as_current_span("z"):
            pass

    def test_helpers_do_not_raise_without_provider(self):
        # No in-memory provider fixture here: exercise the no-op path.
        with traced("noop", foo="bar") as span:
            assert span is not None
        set_span_attributes(a=1, b=None)
