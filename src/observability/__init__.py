"""Agent-side observability helpers (OpenTelemetry spans for trace debugging)."""

from src.observability.tracing import get_tracer, set_span_attributes, traced

__all__ = ["get_tracer", "set_span_attributes", "traced"]
