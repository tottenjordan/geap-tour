"""Lightweight OpenTelemetry helpers for rich agent-side spans.

These helpers make it easy to annotate WHY a request behaved the way it did
(e.g. the router's complexity score, chosen tier, and model) so a Cloud Trace
tells the debugging story at a glance.

Design notes:
- This module never installs a TracerProvider. Deployed Agent Engines get a
  provider from the runtime (``GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY``);
  local runs and tests leave the default no-op provider in place. That makes
  every helper here a transparent no-op unless telemetry is actually on, so it
  is safe to call from hot paths, callbacks, and tests without a collector.
- Mirrors the exporter/idioms in ``src/mcp_servers/otel_setup.py`` (same
  ``opentelemetry.trace`` API) but stays dependency-light: only the OTel API.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

DEFAULT_TRACER_NAME = "geap"


def get_tracer(name: str = DEFAULT_TRACER_NAME) -> trace.Tracer:
    """Return an OTel tracer.

    With no provider configured this returns a no-op tracer, so spans started
    from it never touch a collector — safe in tests and local runs.
    """
    return trace.get_tracer(name)


def set_span_attributes(**attrs: Any) -> None:
    """Set attributes on the currently active span, skipping ``None`` values.

    Safe no-op when there is no active/recording span (the default outside a
    ``traced`` block or when telemetry is disabled).
    """
    span = trace.get_current_span()
    if span is None or not span.is_recording():
        return
    for key, value in attrs.items():
        if value is None:
            continue
        span.set_attribute(key, value)


@contextlib.contextmanager
def traced(
    span_name: str, tracer_name: str = DEFAULT_TRACER_NAME, **attrs: Any
) -> Iterator[Span]:
    """Context manager / decorator that runs a block inside a named span.

    Sets the given attributes (skipping ``None``), records and re-raises any
    exception (``record_exception`` + ERROR status), and yields the span so the
    caller can annotate it with values computed inside the block. Transparent
    no-op when no provider is configured.
    """
    tracer = get_tracer(tracer_name)
    with tracer.start_as_current_span(span_name) as span:
        if span.is_recording():
            for key, value in attrs.items():
                if value is None:
                    continue
                span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
