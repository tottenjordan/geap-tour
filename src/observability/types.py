"""Shapes for the gateway-callout health read."""

from __future__ import annotations

from typing import Literal, TypedDict


class CalloutHealth(TypedDict):
    """Three-valued verdict on whether the gateway's security callouts are running.

    ``no_data`` is NOT ``ok``. No engine here carries an ``agentGatewayConfig``, so
    nothing traverses a gateway and there is nothing to observe — which is
    "unobserved", not "healthy". Collapsing the two would turn this check into one
    that reports green precisely because it is measuring nothing, the vacuous pass
    this repo keeps removing. The ``Literal`` is what keeps the third state from
    being written out of existence.

    ``failed_open`` means a callout errored and the request was allowed through
    anyway — a security control that silently stopped applying.
    """

    verdict: Literal["ok", "failing", "no_data"]
    window_hours: int
    # Floats, not ints: these are sums over Cloud Monitoring series, which are
    # double-valued even when they count discrete events.
    invocation_total: float
    failed_open_total: float
    failed_open_by_status: dict[str, float]
    # A COUNT, despite the name — the producer stores `len(latency)`, not the
    # series. Declared `list[Any]` here from the name alone until ty read the
    # producer. Left as a count rather than renamed (that would change a JSON
    # artifact), but no longer mis-declared.
    latency_series: int
    detail: str
