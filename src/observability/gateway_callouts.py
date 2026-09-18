"""Is the Agent Gateway skipping its own security checks?

Both of our authorization extensions are configured ``failOpen: true`` with a
``timeout: 1s``. When the callout to IAP or Model Armor times out or errors, the
request **proceeds unevaluated** — Layer 1's per-tool conditions do not apply, or the
prompt is never screened. Nothing about the response says so.

Google emits exactly the signal for this, and nothing here read it:

* ``networkservices.googleapis.com/extension/failed_open_count`` — a counter of
  callouts that failed *and were allowed through anyway*, labelled ``ignored_status``
  (``DEADLINE_EXCEEDED``, ``CANCELLED``, …). This is the metric that says "the control
  did not run".
* ``extension/invocation_count`` — total callouts. The denominator, and the thing that
  distinguishes "no failures" from "no traffic".
* ``extension/invocation_latencies`` — how close invocations run to the 1s timeout,
  i.e. how much headroom there is before fail-open starts happening.

**The verdict is three-valued on purpose.** The mistake this module exists to avoid is
reading an empty series as health — the same mistake ``engine_baseline`` made when it
reported a plugin ACTIVE because a flag was set, and the same one
``agent_router/*`` made by alerting on a series with no writer:

``no_data``
    No invocations at all. Expected right now: no engine carries
    ``agentGatewayConfig``, so no traffic traverses a gateway and neither extension has
    ever run. **This is not "healthy"** — it is "unobserved". Exits 0, because it is
    the correct state today, but never claims the controls are working.
``ok``
    Invocations happened and none failed open. The controls ran.
``failing``
    ``failed_open_count > 0``. Requests were allowed past a security control that did
    not evaluate them. Exits non-zero, and names the gRPC statuses responsible.

Usage::

    uv run python -m src.observability.gateway_callouts
    uv run python -m src.observability.gateway_callouts --hours 24 --json
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta

from src.config import GCP_PROJECT_ID
from src.observability.types import CalloutHealth

# Bare metric ids under the networkservices prefix. These are emitted by Google for
# Service Extensions callouts; we never write them.
PREFIX = "networkservices.googleapis.com/"
FAILED_OPEN = "extension/failed_open_count"
INVOCATIONS = "extension/invocation_count"
LATENCIES = "extension/invocation_latencies"

DEFAULT_HOURS = 24


def _client():
    """Lazily construct a MetricServiceClient (import-safe without credentials)."""
    from google.cloud import monitoring_v3

    return monitoring_v3.MetricServiceClient()


def _point_value(point) -> float:
    """A point's numeric value, whichever typed field carries it."""
    value = point.value
    for attr in ("int64_value", "double_value"):
        got = getattr(value, attr, 0)
        if got:
            return float(got)
    # Distribution (latencies) — the count is the useful scalar here.
    dist = getattr(value, "distribution_value", None)
    return float(getattr(dist, "count", 0)) if dist is not None else 0.0


def _series(client, metric: str, hours: int) -> list:
    """Raw TimeSeries for one metric over the trailing window; [] when absent.

    A metric whose descriptor was never materialised raises NotFound rather than
    returning empty, and that is indistinguishable from "no traffic" for our purpose —
    both mean unobserved.
    """
    from google.api_core import exceptions as gexc
    from google.cloud import monitoring_v3

    now = datetime.now(tz=UTC)
    request = {
        "name": f"projects/{GCP_PROJECT_ID}",
        "filter": f'metric.type = "{PREFIX}{metric}"',
        "interval": monitoring_v3.TimeInterval(
            start_time=now - timedelta(hours=hours), end_time=now
        ),
        "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
    }
    try:
        return list(client.list_time_series(request=request))
    except gexc.NotFound:
        return []


def _total(series: list) -> float:
    return sum(_point_value(p) for s in series for p in s.points)


def _by_status(series: list) -> dict[str, float]:
    """failed_open totals split by the gRPC status that caused them."""
    out: dict[str, float] = {}
    for s in series:
        labels = dict(getattr(s.metric, "labels", {}) or {})
        status = labels.get("ignored_status") or "UNKNOWN"
        out[status] = out.get(status, 0.0) + sum(_point_value(p) for p in s.points)
    return {k: v for k, v in out.items() if v}


def read_callout_health(hours: int = DEFAULT_HOURS, *, client=None) -> CalloutHealth:
    """Three-valued verdict on whether the gateway's security callouts are running."""
    client = client or _client()

    failed = _series(client, FAILED_OPEN, hours)
    invoked = _series(client, INVOCATIONS, hours)
    latency = _series(client, LATENCIES, hours)

    failed_total = _total(failed)
    invoked_total = _total(invoked)

    if invoked_total == 0 and failed_total == 0:
        verdict = "no_data"
        detail = (
            "no callouts in the window — no engine carries agentGatewayConfig, so "
            "nothing traverses a gateway. UNOBSERVED, not healthy."
        )
    elif failed_total > 0:
        verdict = "failing"
        detail = (
            f"{failed_total:.0f} of {invoked_total:.0f} callouts FAILED OPEN — those "
            f"requests were allowed past a control that never evaluated them."
        )
    else:
        verdict = "ok"
        detail = f"{invoked_total:.0f} callouts, none failed open."

    return {
        "verdict": verdict,
        "detail": detail,
        "window_hours": hours,
        "failed_open_total": failed_total,
        "invocation_total": invoked_total,
        "failed_open_by_status": _by_status(failed),
        "latency_series": len(latency),
    }


def render(result: CalloutHealth) -> str:
    mark = {"ok": "ok", "failing": "XX", "no_data": "--"}[result["verdict"]]
    lines = [
        f"  {mark} gateway callouts [{result['verdict']}] over {result['window_hours']}h",
        f"     {result['detail']}",
    ]
    for status, count in sorted(result["failed_open_by_status"].items()):
        lines.append(f"     {status}: {count:.0f}")
    if result["verdict"] == "failing":
        lines.append(
            "     Both extensions are failOpen=true with a 1s timeout; see "
            "docs/notes/geap-services-audit-2026-09.md."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, client=None) -> int:
    parser = argparse.ArgumentParser(
        description="Report Agent Gateway authorization-callout failures (fail-open events)."
    )
    parser.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)

    result = read_callout_health(args.hours, client=client)
    print(json.dumps(result, indent=2) if args.json else render(result))
    # Only a real observed failure is an error. `no_data` is the expected state while
    # nothing is attached, and exiting non-zero on it would train everyone to ignore it.
    return 1 if result["verdict"] == "failing" else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
