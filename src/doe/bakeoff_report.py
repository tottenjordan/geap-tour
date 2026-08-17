"""Fuse the coordinator bake-off's four evidence streams into one markdown report.

A bake-off compares two coordinator deployments that differ only by backbone —
``gemini-3.6-flash`` (baseline) vs ``claude-sonnet-5`` (candidate). This module is
pure assembly: it takes four already-computed inputs and renders a report plus a
one-line verdict. No network, no file IO.

Inputs (all keyed by model id where per-model):
* ``quality`` — ``{model_id: {rubric_name: mean_0_1}}`` (offline batch rubrics,
  harvested from the DOE ``results.csv`` — see :func:`quality_from_results_frame`).
* ``pairwise`` — the Phase-4 SxS result
  ``{win_rate_candidate, win_rate_baseline, tie_rate}``.
* ``online`` — ``{model_id: {p50_latency, p95_latency, error_rate}}`` (synthetic
  traffic, from grouped ``verify_monitors`` — see :func:`online_from_grouped_monitors`).
* ``cost`` — ``{model_id: mean_usd_per_request}`` (fair token→$ / GSU cost model).

Any per-model input may be empty (e.g. an offline-only run); missing cells render
as ``n/a`` rather than crashing.
"""

from __future__ import annotations

# Level label (DOE coded factor) -> the online/verify latency+error metric names
# we surface, mapped to the friendlier report keys. The verify_monitors traffic
# surface names p95 latency and error rate; p50 is optional.
_ONLINE_KEYS = {
    "request_latency_p50": "p50_latency",
    "request_latency_p95": "p95_latency",
    "error_rate": "error_rate",
}


def quality_deltas(
    quality: dict[str, dict[str, float]], *, baseline: str, candidate: str
) -> dict[str, float]:
    """Per-rubric ``candidate - baseline`` for every metric present in *both*."""
    base = quality.get(baseline, {})
    cand = quality.get(candidate, {})
    shared = [m for m in cand if m in base]
    return {m: cand[m] - base[m] for m in shared}


def _mean(values) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _cost_ratio(cost: dict[str, float], *, baseline: str, candidate: str) -> float | None:
    base = cost.get(baseline)
    cand = cost.get(candidate)
    if base is None or cand is None or base == 0:
        return None
    return cand / base


def build_verdict(
    quality: dict[str, dict[str, float]],
    pairwise: dict,
    online: dict[str, dict[str, float]],
    cost: dict[str, float],
    *,
    baseline: str,
    candidate: str,
) -> str:
    """One-line human verdict fusing quality delta, SxS, cost, and latency."""
    parts: list[str] = []

    # Quality: average rubric delta across shared metrics.
    deltas = quality_deltas(quality, baseline=baseline, candidate=candidate)
    avg_delta = _mean(deltas.values())
    if avg_delta is not None:
        winner = candidate if avg_delta >= 0 else baseline
        parts.append(f"{winner} wins offline quality by {abs(avg_delta):.3f} avg rubric")

    # Pairwise SxS: whoever has the higher win rate takes the head-to-head.
    cand_wr = pairwise.get("win_rate_candidate")
    base_wr = pairwise.get("win_rate_baseline")
    if cand_wr is not None and base_wr is not None:
        sxs_winner = candidate if cand_wr >= base_wr else baseline
        sxs_wr = max(cand_wr, base_wr)
        # Hedge the head-to-head when the sign test says it's within noise.
        sig = pairwise.get("significance") or {}
        hedge = " (not significant)" if sig and sig.get("significant") is False else ""
        parts.append(f"{sxs_winner} wins SxS at {sxs_wr * 100:.0f}%{hedge}")

    # Cost: candidate-vs-baseline multiple.
    ratio = _cost_ratio(cost, baseline=baseline, candidate=candidate)
    if ratio is not None:
        if ratio >= 1:
            parts.append(f"{candidate} costs {ratio:.1f}x more")
        else:
            parts.append(f"{candidate} costs {ratio:.2f}x ({1 / ratio:.1f}x cheaper)")

    # Latency: candidate p95 delta in ms.
    base_p95 = online.get(baseline, {}).get("p95_latency")
    cand_p95 = online.get(candidate, {}).get("p95_latency")
    if base_p95 is not None and cand_p95 is not None:
        dms = (cand_p95 - base_p95) * 1000
        verb = "adds" if dms >= 0 else "saves"
        parts.append(f"{candidate} {verb} {abs(dms):.0f} ms p95")

    return "; ".join(parts) if parts else "insufficient data for a verdict"


def _fmt(x, spec: str = ".4f") -> str:
    return "n/a" if x is None else format(x, spec)


def _two_col_table(
    title: str,
    rows: list[tuple[str, float | None, float | None]],
    baseline: str,
    candidate: str,
    *,
    delta: bool = False,
) -> list[str]:
    """Render a ``| metric | baseline | candidate | [Δ] |`` markdown table."""
    header = f"| Metric | {baseline} | {candidate} |"
    sep = "|---|---|---|"
    if delta:
        header += " Delta (cand - base) |"
        sep += "---|"
    out = [f"## {title}", "", header, sep]
    for name, bval, cval in rows:
        line = f"| {name} | {_fmt(bval)} | {_fmt(cval)} |"
        if delta:
            d = None if (bval is None or cval is None) else cval - bval
            line += f" {_fmt(d, '+.4f')} |"
        out.append(line)
    out.append("")
    return out


def build_bakeoff_report(
    quality: dict[str, dict[str, float]],
    pairwise: dict,
    online: dict[str, dict[str, float]],
    cost: dict[str, float],
    *,
    baseline: str,
    candidate: str,
    experiment_id: str | None = None,
) -> str:
    """Render the full bake-off report as markdown."""
    title = f"# Coordinator Model Bake-Off: {baseline} vs {candidate}"
    lines = [title, ""]
    if experiment_id:
        lines += [f"- Experiment: `{experiment_id}`", ""]
    lines += [
        f"- Baseline: `{baseline}`",
        f"- Candidate: `{candidate}`",
        "",
    ]

    # Offline quality: union of rubric names across both models, sorted.
    base_q = quality.get(baseline, {})
    cand_q = quality.get(candidate, {})
    rubrics = sorted(set(base_q) | set(cand_q))
    q_rows = [(m, base_q.get(m), cand_q.get(m)) for m in rubrics]
    lines += _two_col_table(
        "Offline Quality (deployed-engine rubrics, 0-1)",
        q_rows,
        baseline,
        candidate,
        delta=True,
    )

    # Pairwise SxS.
    lines += ["## Pairwise Side-by-Side (flip-debiased win rate)", ""]
    cand_wr = pairwise.get("win_rate_candidate")
    base_wr = pairwise.get("win_rate_baseline")
    tie = pairwise.get("tie_rate")
    lines += [
        f"- Candidate ({candidate}) win rate: {_fmt(cand_wr, '.1%')}",
        f"- Baseline ({baseline}) win rate: {_fmt(base_wr, '.1%')}",
        f"- Tie rate: {_fmt(tie, '.1%')}",
        "",
    ]

    # Online traffic stats.
    base_o = online.get(baseline, {})
    cand_o = online.get(candidate, {})
    o_rows = [
        ("p50 latency (s)", base_o.get("p50_latency"), cand_o.get("p50_latency")),
        ("p95 latency (s)", base_o.get("p95_latency"), cand_o.get("p95_latency")),
        ("error rate", base_o.get("error_rate"), cand_o.get("error_rate")),
    ]
    lines += _two_col_table("Online (synthetic traffic)", o_rows, baseline, candidate)

    # Cost.
    c_rows = [("$ / request", cost.get(baseline), cost.get(candidate))]
    lines += _two_col_table("Cost (fair per-request)", c_rows, baseline, candidate)

    # Verdict.
    lines += [
        "## Verdict",
        "",
        build_verdict(quality, pairwise, online, cost, baseline=baseline, candidate=candidate),
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Adapters: extract the per-model inputs from the upstream artifacts.
# --------------------------------------------------------------------------- #


def quality_from_results_frame(df, level_to_model: dict[str, str]) -> dict[str, dict[str, float]]:
    """Per-model rubric means from a harvested DOE results frame.

    Each row is a design point carrying the ``model_backend`` factor level
    (``gemini``/``claude``) plus the batch-metric response columns. Rows are
    grouped by level, averaged, and re-keyed to the real model id via
    ``level_to_model``. Non-numeric / factor columns are ignored.
    """
    from src.doe.harvest import BATCH_METRICS

    out: dict[str, dict[str, float]] = {}
    for level, model_id in level_to_model.items():
        rows = df[df["model_backend"] == level]
        if rows.empty:
            continue
        metrics: dict[str, float] = {}
        for m in BATCH_METRICS:
            if m in rows.columns:
                series = rows[m].dropna()
                if not series.empty:
                    metrics[m] = float(series.astype(float).mean())
        out[model_id] = metrics
    return out


def online_from_grouped_monitors(grouped: dict) -> dict[str, dict[str, float]]:
    """Per-model latency/error stats from a grouped ``verify_monitors`` result.

    Reads any surface with ``group_by`` set: its ``metrics`` map is
    ``{metric_name: {model_id: {"avg_score": float}}}``. Only the latency and
    error-rate metrics named in ``_ONLINE_KEYS`` are surfaced.
    """
    out: dict[str, dict[str, float]] = {}
    for surface in grouped.values():
        if not isinstance(surface, dict) or not surface.get("group_by"):
            continue
        for metric_name, per_model in surface.get("metrics", {}).items():
            key = _ONLINE_KEYS.get(metric_name)
            if key is None or not isinstance(per_model, dict):
                continue
            for model_id, summary in per_model.items():
                if isinstance(summary, dict) and "avg_score" in summary:
                    out.setdefault(model_id, {})[key] = float(summary["avg_score"])
    return out
