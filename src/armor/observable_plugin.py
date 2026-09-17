"""A Model Armor plugin that says whether it BLOCKED or merely BROKE.

ADK's ``ModelArmorPlugin`` returns the *same* text either way::

    _DEFAULT_BLOCKED_MESSAGE = "I'm sorry, but I can't help with that request."

It is returned when screening finds a real violation, and again when the screening
CALL fails and ``block_on_screening_failure`` (default ``True``) trips. On 2026-09-17
a fresh Gemini-3 coordinator answered *every* prompt with it, because its
``AGENT_IDENTITY`` held no ``roles/modelarmor.user`` and the call 403'd. Working
perfectly and completely broken were indistinguishable from the outside, and the only
discriminator was a log line nothing alerted on — so the diagnosis took a day.

This subclass separates the two into distinct signals, mirroring what
``guardrail_with_telemetry`` already does for the client-side blocklist:

* a genuine block joins the existing ``agent_armor/blocked`` series, labelled
  ``reason=model_armor_plugin`` so it is distinguishable from a blocklist hit;
* a screening FAILURE gets its own ``agent_armor/plugin_screening_failed`` series and
  an ``armor.plugin.screening_failed`` span event. A non-zero rate there means the
  agent is refusing traffic it never actually screened.

**The override point is the discriminator.** ``_handle_screening_failure`` is reached
only on a failure — an exception, or a non-``SUCCESS`` ``invocation_result``. A real
violation takes ``_handle_sanitization_result``'s ``MATCH_FOUND`` branch straight to
``_blocked_response``. Splitting anywhere else would conflate them again.

Like ``CachingPreloadMemoryTool``, this subclasses ADK internals and can rot on an ADK
bump; ``tests/test_armor_observable_plugin.py`` pins the two method names against the
real base class so the rot is loud rather than silent.

Telemetry is fully guarded on both paths. A metrics or OTel failure must never change
a screening decision — that is the whole point of a security control.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from google.adk.integrations.model_armor import ModelArmorPlugin
from opentelemetry import trace

if TYPE_CHECKING:  # pragma: no cover - typing only
    from google.adk.models.llm_response import LlmResponse

logger = logging.getLogger(__name__)

# Bare metric types; MetricsWriter normalizes to custom.googleapis.com/.
PLUGIN_FAILED_METRIC = "agent_armor/plugin_screening_failed"

# Reason label for a genuine plugin block, so it lands on the SAME
# agent_armor/blocked series as the client-side guardrail but stays separable.
REASON_PLUGIN_MATCH = "model_armor_plugin"

SCREENING_FAILED_EVENT = "armor.plugin.screening_failed"


def _emit(metric: str, reason: str, event: str, metrics_writer=None) -> None:
    """Span event + metric, each independently guarded.

    Separate ``try`` blocks on purpose: a broken metrics client must not cost us the
    span event, and neither may reach the caller.
    """
    try:
        span = trace.get_current_span()
        if span is not None and span.is_recording():
            span.add_event(event, {"armor.reason": reason})
    except Exception:  # pragma: no cover - defensive
        pass

    try:
        writer = metrics_writer
        if writer is None:
            from src.observability.metrics import MetricsWriter

            writer = MetricsWriter()
        writer.write_gauge(metric, 1, labels={"reason": reason})
    except Exception:  # pragma: no cover - defensive
        pass


class ObservableModelArmorPlugin(ModelArmorPlugin):
    """``ModelArmorPlugin`` that distinguishes a block from a screening failure."""

    def __init__(self, *args: Any, metrics_writer=None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Injectable so tests never construct a real metrics client.
        self._metrics_writer = metrics_writer

    def _handle_screening_failure(self, blocked_message: str) -> LlmResponse | None:
        """Screening did not run. Say so, loudly, then behave exactly as ADK does.

        Reached only on failure. ``super()`` still decides whether to block, so
        ``block_on_screening_failure`` keeps its meaning and this stays observation
        rather than policy.
        """
        logger.error(
            "Model Armor screening FAILED — the agent is about to answer without "
            "having been screened. If this is every request, check that the engine's "
            "AGENT_IDENTITY holds roles/modelarmor.user "
            "(scripts/lib/config.sh:grant_modelarmor_user) and that the engine was "
            "recycled after the grant."
        )
        _emit(
            PLUGIN_FAILED_METRIC,
            "screening_call_failed",
            SCREENING_FAILED_EVENT,
            metrics_writer=self._metrics_writer,
        )
        return super()._handle_screening_failure(blocked_message)

    def _handle_sanitization_result(self, result, **kwargs: Any) -> LlmResponse | None:
        """Count a genuine block, and ONLY a genuine block.

        ADK routes a non-``SUCCESS`` ``invocation_result`` from here into
        ``_handle_screening_failure``, which this class already counts — so the
        condition below is deliberately narrow (``SUCCESS`` *and* ``MATCH_FOUND``)
        rather than "super returned a response". A wider test would double-count a
        failure as both broken and blocked, which is precisely the conflation this
        module exists to undo.
        """
        outcome = super()._handle_sanitization_result(result, **kwargs)
        try:
            from google.cloud import modelarmor_v1

            succeeded = result.invocation_result == modelarmor_v1.InvocationResult.SUCCESS
            matched = result.filter_match_state == modelarmor_v1.FilterMatchState.MATCH_FOUND
            if succeeded and matched:
                from src.armor.config import ARMOR_BLOCKED_METRIC

                _emit(
                    ARMOR_BLOCKED_METRIC,
                    REASON_PLUGIN_MATCH,
                    "guardrail.blocked",
                    metrics_writer=self._metrics_writer,
                )
        except Exception:  # pragma: no cover - defensive
            pass
        return outcome
