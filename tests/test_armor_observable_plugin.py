"""The Model Armor plugin must say whether it BLOCKED or merely BROKE.

ADK returns the same text for both. On 2026-09-17 a fresh Gemini-3 coordinator
answered every prompt with it because its AGENT_IDENTITY could not call Model Armor,
and "working" was indistinguishable from "completely broken" without reading engine
logs.

These drive the REAL plugin through its REAL callbacks with a stubbed Model Armor
client, rather than calling the private handlers directly — per CODE_STANDARDS,
because the thing under test is which path a given outcome takes, and calling the
handler myself would choose the path for it.
"""

from __future__ import annotations

from typing import Any

import pytest
from google.adk.integrations.model_armor import ModelArmorConfig as AdkModelArmorConfig
from google.adk.integrations.model_armor import ModelArmorPlugin
from google.cloud import modelarmor_v1

from src.armor.config import ARMOR_BLOCKED_METRIC
from src.armor.observable_plugin import (
    PLUGIN_FAILED_METRIC,
    REASON_PLUGIN_MATCH,
    ObservableModelArmorPlugin,
)

# No `importorskip`: google-cloud-modelarmor is a declared project dependency
# (pyproject.toml), so a missing import is a broken environment, not a skip.

TEMPLATE = "projects/p/locations/us-central1/templates/t"


class RecordingWriter:
    """Captures metric writes instead of talking to Cloud Monitoring."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, dict]] = []

    def write_gauge(self, metric: str, value: int, labels: dict | None = None) -> None:
        self.writes.append((metric, labels or {}))

    def metrics(self) -> list[str]:
        return [m for m, _ in self.writes]


def _result(*, succeeded: bool = True, matched: bool = False):
    r = modelarmor_v1.SanitizationResult()
    r.invocation_result = (
        modelarmor_v1.InvocationResult.SUCCESS
        if succeeded
        else modelarmor_v1.InvocationResult.INVOCATION_RESULT_UNSPECIFIED
    )
    r.filter_match_state = (
        modelarmor_v1.FilterMatchState.MATCH_FOUND
        if matched
        else modelarmor_v1.FilterMatchState.NO_MATCH_FOUND
    )
    return r


def _plugin(writer, sanitize) -> ObservableModelArmorPlugin:
    """A real plugin whose only stub is the Model Armor call itself."""
    p = ObservableModelArmorPlugin(
        config=AdkModelArmorConfig(prompt_template_name=TEMPLATE, response_template_name=TEMPLATE),
        metrics_writer=writer,
    )
    p._sanitize_user_prompt = sanitize  # type: ignore[method-assign]
    return p


def _request(text: str):
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    return LlmRequest(contents=[types.Content(role="user", parts=[types.Part(text=text)])])


async def _screen(plugin, text: str = "book me a flight"):
    return await plugin.before_model_callback(callback_context=None, llm_request=_request(text))


class TestFailureAndBlockAreDistinguishable:
    """The whole point: same user-visible text, different signals."""

    async def test_a_screening_failure_emits_the_failure_metric(self) -> None:
        """A 403 from Model Armor — the live failure. The agent still refuses (ADK's
        block_on_screening_failure default is untouched), but now it says why."""

        async def boom(*_a: Any, **_k: Any):
            raise PermissionError("403 caller lacks modelarmor.templates.use")

        writer = RecordingWriter()
        out = await _screen(_plugin(writer, boom))

        assert out is not None, "fail-closed behaviour changed; that is policy, not telemetry"
        assert PLUGIN_FAILED_METRIC in writer.metrics()
        assert ARMOR_BLOCKED_METRIC not in writer.metrics(), (
            "a screening FAILURE was counted as a block — the exact conflation this fixes"
        )

    async def test_a_genuine_block_emits_the_blocked_metric(self) -> None:
        """A real jailbreak. Same text to the user, different series."""

        async def matched(*_a: Any, **_k: Any):
            return _result(matched=True)

        writer = RecordingWriter()
        out = await _screen(_plugin(writer, matched))

        assert out is not None
        assert ARMOR_BLOCKED_METRIC in writer.metrics()
        assert PLUGIN_FAILED_METRIC not in writer.metrics()
        assert dict(writer.writes)[ARMOR_BLOCKED_METRIC]["reason"] == REASON_PLUGIN_MATCH

    async def test_a_clean_prompt_emits_nothing(self) -> None:
        """No signal is as important as the other two: a per-request metric on the
        happy path would bury the rate we actually alert on."""

        async def clean(*_a: Any, **_k: Any):
            return _result()

        writer = RecordingWriter()
        assert await _screen(_plugin(writer, clean)) is None
        assert writer.writes == []

    async def test_a_non_success_invocation_counts_once_as_a_failure(self) -> None:
        """ADK routes a non-SUCCESS invocation_result into _handle_screening_failure,
        which this class already counts. Counting "super returned a response" instead
        would double-count it as broken AND blocked."""

        async def unsuccessful(*_a: Any, **_k: Any):
            return _result(succeeded=False)

        writer = RecordingWriter()
        await _screen(_plugin(writer, unsuccessful))

        assert writer.metrics() == [PLUGIN_FAILED_METRIC]


class TestTelemetryCannotChangeTheDecision:
    """A security control must not depend on its own observability."""

    async def test_a_broken_metrics_writer_still_blocks(self) -> None:
        class Exploding:
            def write_gauge(self, *_a: Any, **_k: Any) -> None:
                raise RuntimeError("monitoring is down")

        async def boom(*_a: Any, **_k: Any):
            raise PermissionError("403")

        out = await _screen(_plugin(Exploding(), boom))
        assert out is not None, "a metrics failure suppressed a block"

    async def test_a_broken_metrics_writer_still_allows_a_clean_prompt(self) -> None:
        class Exploding:
            def write_gauge(self, *_a: Any, **_k: Any) -> None:
                raise RuntimeError("monitoring is down")

        async def clean(*_a: Any, **_k: Any):
            return _result()

        assert await _screen(_plugin(Exploding(), clean)) is None


class TestTheAdkSurfaceWeSubclass:
    """Subclassing ADK internals rots on a bump. Make the rot loud.

    Same precedent and same risk as `CachingPreloadMemoryTool`.
    """

    @pytest.mark.parametrize("method", ["_handle_screening_failure", "_handle_sanitization_result"])
    def test_the_overridden_methods_still_exist_upstream(self, method: str) -> None:
        assert hasattr(ModelArmorPlugin, method), (
            f"ADK no longer defines {method}; ObservableModelArmorPlugin is overriding "
            f"a method that does not exist, so its telemetry is silently dead"
        )

    def test_the_factory_returns_the_observable_subclass(self) -> None:
        """A Gemini-3 deploy must get the instrumented plugin, not ADK's."""
        from src.armor.config import model_armor_plugin

        assert isinstance(model_armor_plugin("gemini-3.5-flash"), ObservableModelArmorPlugin)

    def test_fail_closed_is_still_the_default(self) -> None:
        """This module observes; it must not have quietly changed policy."""
        from src.armor.config import model_armor_plugin

        assert model_armor_plugin("gemini-3.5-flash")._config.block_on_screening_failure is True
