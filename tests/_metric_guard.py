"""The stub that makes a live Cloud Monitoring write impossible during tests.

A module rather than a closure inside the fixture so it can be tested directly —
a guard nobody exercises is the thing this repo keeps finding.

**Records, does not raise.** Publishing in `src/` is guarded telemetry: every path
swallows its own exceptions so a metrics outage can never abort an eval run. A stub
that raises is therefore absorbed and the test still passes — verified, the suite
reported 2273 passed with a raising stub installed while still writing. Recording
and asserting afterwards is the only form the swallow cannot defeat.
"""

from __future__ import annotations


class ForbiddenMetricClient:
    """Stands in for ``monitoring_v3.MetricServiceClient`` and logs write attempts.

    Construction is what gets intercepted, so a test that injects its own fake
    client never reaches this. Reads raise immediately — a test reading live
    Monitoring is a different problem, but not one to discover silently.
    """

    def __init__(self, attempts: list[str] | None = None, *_a, **_k) -> None:
        self.attempts: list[str] = [] if attempts is None else attempts

    def create_time_series(self, name=None, time_series=None, **_k) -> None:
        if not time_series:
            self.attempts.append("<empty write>")
            return
        for ts in time_series:
            self.attempts.append(getattr(getattr(ts, "metric", None), "type", "<unknown>"))

    def __getattr__(self, item: str):
        raise AssertionError(
            f"a test reached the real Cloud Monitoring client (.{item}). "
            "Inject a fake client or patch the publish function."
        )


def failure_message(attempts: list[str]) -> str:
    return (
        "this test wrote to LIVE Cloud Monitoring: "
        + ", ".join(sorted(set(attempts)))
        + ". Pass writer=/client= a fake, or patch the publish function. Publishing is "
        "guarded telemetry, so the write will not fail loudly on its own."
    )
