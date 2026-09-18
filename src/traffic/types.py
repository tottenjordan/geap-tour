"""Shapes for the synthetic-load results.

``generate_load`` is the only thing in this repo that drives an engine at a chosen
rate, so its numbers are what "the engine is healthy under load" means here.
``offered`` vs ``sent`` vs ``achieved_qps`` are three different facts and the
distinction is the point: the shared session-creation rate — not per-replica
compute — is the throughput ceiling, so a run can offer 3 QPS, send far fewer, and
raising ``min_instances`` will not change it.
"""

from __future__ import annotations

from typing import NotRequired, TypedDict


class LoadResult(TypedDict):
    """One load stage's outcome.

    ``injected`` counts faults this run deliberately caused, kept apart from
    ``errors`` so an induced failure is never read as engine degradation.
    """

    offered: int
    sent: int
    errors: int
    injected: int
    duration_s: float
    achieved_qps: float
    p50_latency: float
    p95_latency: float
    # Stamped only when this result is one stage of a ramp, so a standalone run
    # cannot be mistaken for stage 0 of a profile that never happened.
    stage: NotRequired[int]
    target_qps: NotRequired[float]


class ScalingProfile(TypedDict):
    """A multi-stage ramp. ``peak_qps`` is the ACHIEVED peak, not the offered one."""

    stages: list[LoadResult]
    total_offered: int
    total_sent: int
    total_errors: int
    total_injected: int
    peak_qps: float
