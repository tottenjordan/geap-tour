"""Diagnostic: is the residual empty-at-200 rate a concurrency effect?

The coordinator's empty rate swings **4%-27% across runs of the same 49 cases on the
same warm (`min_instances=4`) engine**. Two runs shared only 2 of their 13 and 6 empty
cases, so it is not the agent declining particular prompts — it is stochastic, i.e.
infra. Four empty-at-200 causes are already documented and fixed
(docs/notes/empty-at-200-field-guide.md); none explains this one.

This sweeps **one variable** — inference concurrency — and reports whether the rate
tracks it:

* rate ≈ 0 at ``workers=1`` and rising  ⇒ scale-out contention; the levers are
  throttling, warming, and an empty-rate ceiling in ``demo_readiness``.
* rate flat and non-zero across levels  ⇒ a steady-state engine defect; concurrency
  is not the lever and it needs escalating.

Three design points that make the result trustworthy:

1. **Inference only.** The empty rate is a property of inference; running the judge
   phase would multiply cost and time for no extra signal.
2. **Subprocess per arm.** ``_sdk_patches._AGENT_MAX_WORKERS`` is read at *import
   time*, so ``EVAL_AGENT_MAX_WORKERS`` must be set before the child imports —
   setting it in-process after import does nothing. Same reason
   :mod:`src.doe.launch` uses subprocess-per-point.
3. **Replicate runs, not items.** The existing data is *overdispersed*: if empties
   were i.i.d. per item at p≈0.14, a 49-item run would have SE≈5%, yet the observed
   spread is ~4.6 SE wide. Empties cluster *within* a run, so the unit of replication
   has to be the run. ``empty_indices`` is reported so that clustering can be checked
   directly — contiguous indices point at a replica failing mid-run, scattered ones
   at per-request randomness.

**Read-only**: drives the engine, writes no metrics and no files.

Usage:
  uv run python -m src.eval.sweep_empty_rate --agent-id <ENGINE_ID> --dry-run
  uv run python -m src.eval.sweep_empty_rate --agent-id <ENGINE_ID> --workers 1,4,8 --repeats 3
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING

from src.eval.stats import wilson_ci

if TYPE_CHECKING:
    from collections.abc import Sequence

# The child prints exactly one line with this prefix; the parent parses it.
RESULT_MARKER = "SWEEP_RESULT: "


def arm_order(workers: Sequence[int], repeats: int) -> list[int]:
    """Interleaved arm order, so time-of-day drift cannot alias onto a level.

    ``[1,4,8] x3`` becomes ``1,4,8, 4,8,1, 8,1,4`` — each level appears once per
    block and in a different position each block. Running all repeats of one level
    together would confound "workers=1" with "the first 10 minutes".
    """
    levels = list(workers)
    order: list[int] = []
    for block in range(repeats):
        order.extend(levels[block % len(levels) :] + levels[: block % len(levels)])
    return order


def summarize(results: list[dict]) -> dict[int, dict]:
    """Aggregate per-arm: pooled rate + interval, and the per-run rates behind it."""
    out: dict[int, dict] = {}
    for workers in sorted({r["workers"] for r in results}):
        runs = [r for r in results if r["workers"] == workers]
        empty = sum(r["empty"] for r in runs)
        total = sum(r["n"] for r in runs)
        rates = [r["empty"] / r["n"] for r in runs if r["n"]]
        low, high = wilson_ci(empty, total)
        out[workers] = {
            "runs": len(runs),
            "empty": empty,
            "total": total,
            "rate": empty / total if total else 0.0,
            "ci": (low, high),
            "per_run": rates,
            "spread": (max(rates) - min(rates)) if rates else 0.0,
            "exhausted": sum(r.get("exhausted", 0) for r in runs),
            "empty_attempts": sum(r.get("empty_attempts", 0) for r in runs),
        }
    return out


def verdict(summary: dict[int, dict]) -> str:
    """Read the sweep against criteria fixed before the numbers were seen."""
    if len(summary) < 2:
        return "INCONCLUSIVE — need at least two concurrency levels."
    levels = sorted(summary)
    lo, hi = summary[levels[0]], summary[levels[-1]]
    # Overlapping intervals mean the sweep cannot separate the arms, whatever the
    # point estimates suggest. Say so rather than reading tea leaves.
    if lo["ci"][1] >= hi["ci"][0] and hi["ci"][1] >= lo["ci"][0]:
        return (
            "NO DETECTABLE EFFECT at this power — the arms' Wilson intervals overlap. "
            "Add repeats or accept that concurrency is not the dominant factor; do "
            "NOT read the point estimates as a trend."
        )
    if hi["rate"] > lo["rate"] and lo["rate"] < 0.05:
        return (
            "SCALE-OUT CONTENTION — near-zero serially, rising with concurrency. "
            "Levers: throttle EVAL_AGENT_MAX_WORKERS, warm harder, assert an "
            "empty-rate ceiling in demo_readiness."
        )
    if hi["rate"] > lo["rate"]:
        return "CONCURRENCY-SENSITIVE, but non-zero even serially — a floor plus a contention component."
    return "STEADY-STATE DEFECT — the rate does not track concurrency. Escalate; concurrency is not the lever."


def render(results: list[dict], summary: dict[int, dict]) -> str:
    lines = ["=" * 68, "EMPTY-RATE CONCURRENCY SWEEP", "=" * 68]
    lines.append(f"  {'workers':>8}{'runs':>6}{'empty/total':>14}{'rate':>8}{'95% CI':>16}")
    for workers, s in summary.items():
        ci = f"{s['ci'][0]:.0%}-{s['ci'][1]:.0%}"
        fraction = f"{s['empty']}/{s['total']}"
        lines.append(f"  {workers:>8}{s['runs']:>6}{fraction:>14}{s['rate']:>8.0%}{ci:>16}")
    lines.append("")
    lines.append("  per-run rates (overdispersion check — wide spread = run-level clustering):")
    for workers, s in summary.items():
        per = ", ".join(f"{r:.0%}" for r in s["per_run"])
        lines.append(f"    workers={workers}: [{per}]  spread={s['spread']:.0%}")
    lines.append("")
    lines.append("  retries (empties that HAPPENED vs empties that SURVIVED):")
    for workers, s in summary.items():
        lines.append(
            f"    workers={workers}: empty_attempts={s['empty_attempts']:<5} exhausted={s['exhausted']}"
        )
    lines.append("")
    lines.append("  empty item indices per run (contiguous => a replica died mid-run):")
    for r in results:
        lines.append(f"    workers={r['workers']}  {r.get('empty_indices')}")
    lines += ["", f"  VERDICT: {verdict(summary)}"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- child


def run_one_arm(agent_id: str, limit: int | None) -> dict:
    """Inference-only pass in THIS process; the env knob must already be baked."""
    import vertexai

    from src.config import GCP_PROJECT_ID, GCP_REGION
    from src.eval import _sdk_patches
    from src.eval._sdk_patches import warm_agent_engine
    from src.eval.multi_agent_batch_eval import (
        _build_eval_dataset,
        _resolve_agent_resource_name,
        _select_cases,
        count_empty_response_items,
    )
    from src.eval.online_monitor import is_infra_empty

    from agentplatform import Client  # isort: skip  (after patches are applied)

    _sdk_patches.reset_retry_counters()
    vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
    client = Client(project=GCP_PROJECT_ID, location=GCP_REGION)
    arn = _resolve_agent_resource_name(agent_id)

    try:
        warm_agent_engine(client.agent_engines.get(name=arn))
    except Exception as exc:  # warmup is best-effort, exactly as in the real eval
        print(f"  warmup skipped: {exc}", file=sys.stderr)

    df = _build_eval_dataset(_select_cases("coordinator_agent", limit))
    result = client.evals.run_inference(agent=arn, src=df)
    frame = getattr(result, "eval_dataset_df", None)
    empty, total = count_empty_response_items(frame)
    indices = (
        [i for i, cell in enumerate(frame["response"]) if is_infra_empty(cell)]
        if frame is not None and "response" in getattr(frame, "columns", [])
        else []
    )
    return {
        "workers": _sdk_patches._AGENT_MAX_WORKERS,
        "n": total,
        "empty": empty,
        "empty_indices": indices,
        **_sdk_patches.retry_counters(),
    }


# -------------------------------------------------------------------------- parent


def _spawn(workers: int, agent_id: str, limit: int | None) -> dict | None:
    env = {**os.environ, "EVAL_AGENT_MAX_WORKERS": str(workers)}
    cmd = [sys.executable, "-m", "src.eval.sweep_empty_rate", "--_child", "--agent-id", agent_id]
    if limit:
        cmd += ["--limit", str(limit)]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    for line in proc.stdout.splitlines():
        if line.startswith(RESULT_MARKER):
            return json.loads(line[len(RESULT_MARKER) :])
    print(f"  arm workers={workers} produced no result:\n{proc.stderr[-600:]}", file=sys.stderr)
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", default=None)
    parser.add_argument("--workers", default="1,4,8", help="comma-separated concurrency levels")
    parser.add_argument("--repeats", type=int, default=3, help="runs per level")
    parser.add_argument("--limit", type=int, default=None, help="cap the case count")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, touch nothing")
    parser.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    from src.config import AGENT_ENGINE_ID

    agent_id = args.agent_id or AGENT_ENGINE_ID

    if args._child:
        print(RESULT_MARKER + json.dumps(run_one_arm(agent_id, args.limit)))
        return 0

    levels = [int(w) for w in args.workers.split(",") if w.strip()]
    order = arm_order(levels, args.repeats)

    if args.dry_run:
        print(f"DRY RUN — {len(order)} inference passes against {agent_id}, no judging.")
        print(f"  arm order (interleaved): {order}")
        print("  each arm: warm engine, run_inference, count empties + retry telemetry")
        return 0

    results: list[dict] = []
    for i, workers in enumerate(order, 1):
        print(f"[{i}/{len(order)}] workers={workers} ...", flush=True)
        row = _spawn(workers, agent_id, args.limit)
        if row:
            results.append(row)
            print(f"      empty {row['empty']}/{row['n']}  exhausted={row.get('exhausted')}")
    if not results:
        print("No arm produced a result.")
        return 1
    print()
    print(render(results, summarize(results)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
