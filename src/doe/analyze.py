"""Analyze a harvested DOE table: main effects, factor ranking, and the
cost-quality frontier; render a markdown report.

Main effect of a two-level factor on a response = mean(response | high level) -
mean(response | low level), computed with nanmean so partial runs still count.
High/low levels come from the factor registry (labels[1]/labels[0]).
"""

from __future__ import annotations

import os

import numpy as np
import pandas as pd

from src.config import GCP_STAGING_BUCKET
from src.doe.factors import Factor
from src.doe.harvest import BATCH_METRICS

# Response columns we report on, in display order.
RESPONSES = (
    *BATCH_METRICS,
    "routing_accuracy",
    "savings_pct",
    "routed_cost_usd",
    "sim_passed",
)

# The metrics the screening experiment is meant to move (below-bar on the
# validated run) — used for factor ranking and the recommended config.
QUALITY_METRICS = ("tool_use_quality", "final_response_match")


def main_effect(df: pd.DataFrame, factor: Factor, response: str) -> float:
    """mean(response | high) - mean(response | low) for one factor/response."""
    if factor.name not in df.columns or response not in df.columns:
        return float("nan")
    high = df.loc[df[factor.name] == factor.high_label, response]
    low = df.loc[df[factor.name] == factor.low_label, response]
    if high.empty or low.empty:
        return float("nan")
    return float(np.nanmean(high.to_numpy(dtype=float))) - float(
        np.nanmean(low.to_numpy(dtype=float))
    )


def main_effects_table(
    df: pd.DataFrame, factors: list[Factor], responses=RESPONSES
) -> pd.DataFrame:
    """Rows = factors, columns = responses, values = main effects."""
    data = {
        r: [main_effect(df, f, r) for f in factors] for r in responses
    }
    return pd.DataFrame(data, index=[f.name for f in factors])


def rank_factors(df: pd.DataFrame, factors: list[Factor], response: str):
    """Factors ranked by |main effect| on a response, largest first."""
    effects = [(f.name, main_effect(df, f, response)) for f in factors]
    return sorted(
        effects,
        key=lambda kv: (float("-inf") if np.isnan(kv[1]) else abs(kv[1])),
        reverse=True,
    )


def _quality_score(df: pd.DataFrame) -> pd.Series:
    """Per-row mean of the target quality metrics (nan-aware)."""
    cols = [m for m in QUALITY_METRICS if m in df.columns]
    if not cols:
        return pd.Series([float("nan")] * len(df), index=df.index)
    return df[cols].astype(float).mean(axis=1, skipna=True)


def cost_quality_frontier(df: pd.DataFrame) -> pd.DataFrame:
    """Design points that are Pareto-optimal on (savings_pct↑, quality↑)."""
    if "savings_pct" not in df.columns:
        return df.iloc[0:0]
    work = df.copy()
    work["_quality"] = _quality_score(work)
    work = work.dropna(subset=["savings_pct", "_quality"])
    keep = []
    for i, row in work.iterrows():
        dominated = (
            (work["savings_pct"] >= row["savings_pct"])
            & (work["_quality"] >= row["_quality"])
            & (
                (work["savings_pct"] > row["savings_pct"])
                | (work["_quality"] > row["_quality"])
            )
        ).any()
        if not dominated:
            keep.append(i)
    return work.loc[keep, ["design_point", "savings_pct", "_quality"]].rename(
        columns={"_quality": "quality_score"}
    )


def recommend_config(df: pd.DataFrame, factors: list[Factor]) -> dict[str, str]:
    """Level of each factor that maximizes the mean quality score."""
    work = df.copy()
    work["_quality"] = _quality_score(work)
    rec: dict[str, str] = {}
    for f in factors:
        if f.name not in work.columns:
            continue
        means = {
            label: float(
                np.nanmean(
                    work.loc[work[f.name] == label, "_quality"].to_numpy(dtype=float)
                )
            )
            if (work[f.name] == label).any()
            else float("nan")
            for label in f.labels
        }
        best = max(
            means,
            key=lambda label: (float("-inf") if np.isnan(means[label]) else means[label]),
        )
        rec[f.name] = best
    return rec


def _fmt(x: float) -> str:
    return "n/a" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:+.4f}"


def build_report(df: pd.DataFrame, factors: list[Factor], experiment_id: str) -> str:
    """Render the analysis as markdown."""
    lines = [
        f"# DOE Analysis — {experiment_id}",
        "",
        f"- Design points: **{len(df)}**",
        f"- Factors: {', '.join(f.name for f in factors)}",
        "",
        "## Main effects (mean high - mean low)",
        "",
    ]
    eff = main_effects_table(df, factors)
    header = "| factor | " + " | ".join(eff.columns) + " |"
    sep = "|" + "---|" * (len(eff.columns) + 1)
    lines += [header, sep]
    for fname, row in eff.iterrows():
        lines.append(
            f"| {fname} | " + " | ".join(_fmt(row[c]) for c in eff.columns) + " |"
        )
    lines.append("")

    lines.append("## Highest-leverage factors for weak metrics")
    lines.append("")
    for metric in QUALITY_METRICS:
        ranked = rank_factors(df, factors, metric)
        lines.append(f"**{metric}**")
        for name, e in ranked:
            lines.append(f"- {name}: {_fmt(e)}")
        lines.append("")

    lines.append("## Cost-quality frontier (savings_pct↑, quality↑)")
    lines.append("")
    frontier = cost_quality_frontier(df)
    if frontier.empty:
        lines.append("_no complete rows to compute a frontier_")
    else:
        lines.append("| design_point | savings_pct | quality_score |")
        lines.append("|---|---|---|")
        for _, r in frontier.iterrows():
            lines.append(
                f"| {r['design_point']} | {r['savings_pct']:.1f} | {r['quality_score']:.4f} |"
            )
    lines.append("")

    rec = recommend_config(df, factors)
    lines.append("## Recommended config (maximizes mean quality score)")
    lines.append("")
    for name, label in rec.items():
        lines.append(f"- **{name}** = `{label}`")
    lines.append("")
    return "\n".join(lines)


def analyze(
    df: pd.DataFrame,
    factors: list[Factor],
    experiment_id: str,
    *,
    out_dir: str = ".",
) -> str:
    """Build the report, write it local + (best-effort) GCS, return the markdown."""
    md = build_report(df, factors, experiment_id)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "report.md")
    with open(path, "w") as f:
        f.write(md)

    prefix = f"eval-results/doe/{experiment_id}"
    try:
        from google.cloud import storage

        storage.Client().bucket(GCP_STAGING_BUCKET).blob(
            f"{prefix}/report.md"
        ).upload_from_filename(path)
        print(f"analysis → gs://{GCP_STAGING_BUCKET}/{prefix}/report.md")
    except Exception as e:
        print(f"report upload skipped: {e}")
    return md
