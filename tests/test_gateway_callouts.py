"""Silence must not read as health.

`gateway_callouts` exists because both authz extensions are `failOpen: true` with a
1s timeout: when the callout to IAP or Model Armor fails, the request proceeds
unevaluated and nothing about the response says so. Google emits
`extension/failed_open_count` for exactly this, and nothing here read it.

The property these tests defend is the three-valued verdict. An empty series means
UNOBSERVED, not OK — the same distinction `engine_baseline` missed when it reported a
Model Armor plugin ACTIVE on the strength of a flag, and the same one `agent_router/*`
missed by alerting on a series nothing wrote to.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.observability import gateway_callouts as gc


class FakePoint:
    def __init__(self, value: float, *, distribution: bool = False) -> None:
        if distribution:
            self.value = SimpleNamespace(
                int64_value=0, double_value=0, distribution_value=SimpleNamespace(count=value)
            )
        else:
            self.value = SimpleNamespace(int64_value=int(value), double_value=0)


class FakeSeries:
    def __init__(self, points: list[float], labels: dict | None = None) -> None:
        self.points = [FakePoint(p) for p in points]
        self.metric = SimpleNamespace(labels=labels or {})


class FakeClient:
    """Returns canned series per metric type, like the real list_time_series."""

    def __init__(self, by_metric: dict[str, list[FakeSeries]]) -> None:
        self.by_metric = by_metric
        self.queried: list[str] = []

    def list_time_series(self, request):
        metric = request["filter"].split('"')[1]
        self.queried.append(metric)
        return self.by_metric.get(metric, [])


def _client(*, failed=None, invoked=None, latency=None) -> FakeClient:
    return FakeClient(
        {
            f"{gc.PREFIX}{gc.FAILED_OPEN}": failed or [],
            f"{gc.PREFIX}{gc.INVOCATIONS}": invoked or [],
            f"{gc.PREFIX}{gc.LATENCIES}": latency or [],
        }
    )


class TestTheThreeValuedVerdict:
    def test_no_traffic_is_no_data_not_ok(self) -> None:
        """THE property. Today nothing is attached to a gateway, so every series is
        empty — and an empty series is the absence of evidence, not evidence of
        health. Reporting `ok` here would be a green check for a control that has
        never run once."""
        result = gc.read_callout_health(client=_client())
        assert result["verdict"] == "no_data"
        assert result["verdict"] != "ok"
        assert "UNOBSERVED" in result["detail"]

    def test_invocations_with_no_failures_is_ok(self) -> None:
        result = gc.read_callout_health(client=_client(invoked=[FakeSeries([40, 60])]))
        assert result["verdict"] == "ok"
        assert result["invocation_total"] == 100

    def test_any_fail_open_is_failing(self) -> None:
        """A single fail-open is a request that skipped a security control."""
        result = gc.read_callout_health(
            client=_client(
                invoked=[FakeSeries([100])],
                failed=[FakeSeries([1], {"ignored_status": "DEADLINE_EXCEEDED"})],
            )
        )
        assert result["verdict"] == "failing"
        assert result["failed_open_total"] == 1

    def test_failures_are_broken_down_by_grpc_status(self) -> None:
        """DEADLINE_EXCEEDED (the 1s timeout) and a hard error want different fixes."""
        result = gc.read_callout_health(
            client=_client(
                invoked=[FakeSeries([100])],
                failed=[
                    FakeSeries([3], {"ignored_status": "DEADLINE_EXCEEDED"}),
                    FakeSeries([2], {"ignored_status": "CANCELLED"}),
                ],
            )
        )
        assert result["failed_open_by_status"] == {"DEADLINE_EXCEEDED": 3.0, "CANCELLED": 2.0}

    def test_failures_without_invocations_still_fail(self) -> None:
        """Defensive: if the denominator is missing but failures are recorded, the
        failures are what matter. `no_data` must not swallow them."""
        result = gc.read_callout_health(
            client=_client(failed=[FakeSeries([2], {"ignored_status": "UNKNOWN"})])
        )
        assert result["verdict"] == "failing"


class TestTheExitCode:
    """A gate is only useful if it fires on the right thing."""

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({}, 0),  # no_data — expected today, must not cry wolf
            ({"invoked": [FakeSeries([10])]}, 0),  # ok
            (
                {"invoked": [FakeSeries([10])], "failed": [FakeSeries([1], {})]},
                1,
            ),  # failing
        ],
        ids=["no_data", "ok", "failing"],
    )
    def test_only_an_observed_failure_is_an_error(self, kwargs, expected, capsys) -> None:
        assert gc.main(["--json"], client=_client(**kwargs)) == expected

    def test_no_data_does_not_exit_non_zero(self) -> None:
        """Exiting non-zero on the expected state trains everyone to ignore it —
        which is how `agent_router/*`'s alerts became noise."""
        assert gc.main([], client=_client()) == 0


class TestItQueriesTheMetricsGoogleActuallyEmits:
    def test_it_reads_all_three_series(self) -> None:
        client = _client()
        gc.read_callout_health(client=client)
        assert client.queried == [
            f"{gc.PREFIX}{gc.FAILED_OPEN}",
            f"{gc.PREFIX}{gc.INVOCATIONS}",
            f"{gc.PREFIX}{gc.LATENCIES}",
        ]

    def test_the_metric_names_are_the_networkservices_ones(self) -> None:
        """These are emitted by Google for Service Extensions callouts — we never
        write them, so a typo yields an empty series that reads as `no_data` forever."""
        assert gc.PREFIX == "networkservices.googleapis.com/"
        assert gc.FAILED_OPEN == "extension/failed_open_count"
        assert gc.INVOCATIONS == "extension/invocation_count"

    def test_a_missing_descriptor_is_empty_not_an_exception(self) -> None:
        """A metric nothing has ever written raises NotFound rather than returning
        empty; that is still 'unobserved' and must not abort the read."""
        from google.api_core import exceptions as gexc

        class NotFoundClient:
            def list_time_series(self, request):
                raise gexc.NotFound("never materialised")

        assert gc.read_callout_health(client=NotFoundClient())["verdict"] == "no_data"


class TestTheRenderedOutput:
    def test_failing_output_names_the_posture_that_allowed_it(self) -> None:
        result = gc.read_callout_health(
            client=_client(
                invoked=[FakeSeries([9])],
                failed=[FakeSeries([1], {"ignored_status": "DEADLINE_EXCEEDED"})],
            )
        )
        text = gc.render(result)
        assert "failing" in text
        assert "DEADLINE_EXCEEDED" in text
        assert "failOpen=true" in text, "the output does not say why it was allowed"

    def test_no_data_output_does_not_look_like_a_pass(self) -> None:
        text = gc.render(gc.read_callout_health(client=_client()))
        assert "no_data" in text
        assert "UNOBSERVED" in text
