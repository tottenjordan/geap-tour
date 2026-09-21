"""The guard that stops the test suite writing to production monitoring.

`uv run pytest tests/ -n 8 --dist loadfile` — the command CLAUDE.md recommends —
published junk points into `custom.googleapis.com/agent_router/cost_savings_pct`,
a series carrying a live alert policy. Four tests called
`run_all_evals._run_publish_phase` with only half the publish path stubbed; two
writes landed and two were deduped by the API. The pair that landed dragged the
rolling baseline to **z = -3.33**, and `verify_monitors` duly reported an anomaly
that nothing deployed had caused.

It hid for a reason that also defeats the obvious fix: publishing is **guarded
telemetry**. Every publish path swallows its own exceptions so a metrics outage can
never abort an eval run. So the suite was green three ways —

* CI has no credentials: the write fails, the failure is swallowed, green.
* Locally there are credentials: the write succeeds, green.
* A guard that makes the client RAISE is swallowed too — tried it, 2273 passed
  while the writes kept happening.

Record-and-assert is the only form that survives, because nothing is thrown and the
assertion runs after the test body, where no `except` can reach it.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from tests._metric_guard import ForbiddenMetricClient, failure_message

_TESTS = pathlib.Path(__file__).resolve().parent


class _Series:
    def __init__(self, metric_type):
        self.metric = type("M", (), {"type": metric_type})()


class TestTheStubRecordsInsteadOfRaising:
    def test_a_write_is_recorded(self):
        attempts: list[str] = []
        ForbiddenMetricClient(attempts).create_time_series(
            name="projects/p", time_series=[_Series("custom.googleapis.com/agent_router/x")]
        )
        assert attempts == ["custom.googleapis.com/agent_router/x"]

    def test_it_does_not_raise_so_a_swallow_cannot_hide_it(self):
        """THE property. The production publish paths are `try/except: pass`; a
        raising stub is absorbed by them and the test passes while still writing."""
        attempts: list[str] = []
        client = ForbiddenMetricClient(attempts)
        try:
            client.create_time_series(name="p", time_series=[_Series("m")])
        except Exception:  # what every publish path in src/ does
            pytest.fail("the stub raised; a guarded caller would have swallowed it")
        assert attempts, "the attempt must survive the caller's except block"

    def test_an_empty_write_still_counts(self):
        """A publish that sends no series is still a live client call, and a stub
        that ignored it would let the next refactor slip through."""
        attempts: list[str] = []
        ForbiddenMetricClient(attempts).create_time_series(name="p", time_series=[])
        assert attempts == ["<empty write>"]

    def test_a_read_is_refused_loudly(self):
        """Reads are a different problem, but not one to discover silently."""
        with pytest.raises(AssertionError, match="real Cloud Monitoring client"):
            ForbiddenMetricClient([]).list_time_series(name="p")

    def test_the_message_names_the_series(self):
        msg = failure_message(["custom.googleapis.com/agent_router/cost_savings_pct"])
        assert "cost_savings_pct" in msg
        assert "guarded telemetry" in msg, "the reader needs to know why it was silent"


class TestTheGuardIsActuallyWiredIn:
    """A fixture that works but is not autouse protects nothing."""

    def test_the_fixture_is_autouse(self):
        src = (_TESTS / "conftest.py").read_text()
        i = src.index("def _no_live_metric_writes")
        assert "autouse=True" in src[max(0, i - 200) : i]

    def test_the_fixture_asserts_rather_than_only_patching(self):
        """Patching without the post-hoc assertion would silence the write and
        report nothing — quieter, and just as blind."""
        src = (_TESTS / "conftest.py").read_text()
        fn = next(
            n
            for n in ast.parse(src).body
            if isinstance(n, ast.FunctionDef) and n.name == "_no_live_metric_writes"
        )
        assert any(isinstance(n, ast.Assert) for n in ast.walk(fn)), (
            "the fixture patches the client but never checks whether it was used"
        )

    def test_this_very_test_run_is_protected(self):
        """End to end: inside a real test, the client is already the stub."""
        from google.cloud import monitoring_v3

        assert isinstance(monitoring_v3.MetricServiceClient(), ForbiddenMetricClient)


class TestTheOriginalOffenderStaysFixed:
    def test_the_report_builder_stubs_the_router_publish(self):
        """`_run_publish_phase` publishes TWO surfaces. Stubbing one is what caused
        this; the guard would catch a regression, but name the file so the next
        reader knows why that fixture exists."""
        src = (_TESTS / "test_report_builder.py").read_text()
        assert "publish_router_efficiency" in src
