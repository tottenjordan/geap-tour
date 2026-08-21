"""Multi-agent batch evaluation — runs batch evals per agent with consolidated output.

Extends the single-agent batch_eval.py pattern to evaluate coordinator, travel,
expense, and router agents independently with agent-appropriate metrics.

Usage:
    uv run python -m src.eval.multi_agent_batch_eval
    uv run python -m src.eval.multi_agent_batch_eval --agents coordinator_agent,travel_agent
    uv run python -m src.eval.multi_agent_batch_eval --list-cases
    uv run python -m src.eval.multi_agent_batch_eval --threshold 3.5
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import vertexai
from agentplatform import Client, types

from src.config import (
    AGENT_ENGINE_ID,
    EVAL_OUTPUT_DIR,
    GCP_PROJECT_ID,
    GCP_REGION,
    GCP_STAGING_BUCKET,
    ROUTER_ENGINE_ID,
)
from src.eval._sdk_patches import patch_evals_sdk, warm_agent_engine
from src.eval.agent_eval_configs import (
    ALL_AGENTS,
    get_eval_cases,
    get_metrics,
)
from src.eval.eval_experiment import (
    ensure_eval_experiment,
    eval_run_display_name,
    eval_run_labels,
)

# Fix the evals SDK for Gemini 3.x responses (thought-signature function calls)
# and result loading before any inference/evaluation runs. See _sdk_patches.py.
patch_evals_sdk()

GCS_EVAL_DEST = f"gs://{GCP_STAGING_BUCKET}/eval-results/"
MAX_POLL_SECONDS = 1200

# The dataframe column the evals SDK stores parsed AgentData under
# (``agentplatform._genai._evals_constant.AGENT_DATA``). Hard-coded rather than
# imported so a private-constant rename degrades to "0 tool calls found" — a
# visible message — instead of an ImportError at module load.
_AGENT_DATA_COLUMN = "agent_data"


def _agent_data_events(cell) -> list[dict]:
    """Flatten one ``agent_data`` cell into a ``stream_query``-shaped event list.

    Accepts the dict our patched parser builds
    (:func:`src.eval._sdk_patches._patch_single_turn_parser`) or the JSON string
    it stores on the error path. Returns ``[]`` for anything unparseable, so a
    malformed row is counted as tool-free rather than crashing the run.
    """
    if isinstance(cell, str):
        try:
            cell = json.loads(cell)
        except (TypeError, ValueError):
            return []
    if not isinstance(cell, dict):
        return []
    return [
        event
        for turn in cell.get("turns") or []
        for event in (turn or {}).get("events") or []
        if isinstance(event, dict)
    ]


def count_tool_call_items(inference_df) -> tuple[int, int]:
    """``(items with >=1 tool event, total items)`` in an inference dataframe.

    ``tool_use_quality_v1`` is scored from the ``AgentData`` events, not the
    response text, and needs at least one ``function_call``/``function_response``
    *somewhere in the run*. Counting here turns an opaque downstream service
    error into a number we can report. Transfers count — the metric sees any
    function call — so this deliberately passes ``include_transfers=True``.
    """
    if inference_df is None or not len(inference_df):
        return 0, 0
    total = len(inference_df)
    if _AGENT_DATA_COLUMN not in getattr(inference_df, "columns", []):
        return 0, total

    from src.eval.trajectory_eval import extract_trajectory, returned_tool_names

    with_calls = 0
    for cell in inference_df[_AGENT_DATA_COLUMN]:
        events = _agent_data_events(cell)
        if extract_trajectory(events, include_transfers=True) or returned_tool_names(events):
            with_calls += 1
    return with_calls, total


def drop_tool_use_metric_if_unscorable(metrics: list, with_calls: int, total: int) -> list:
    """Remove ``TOOL_USE_QUALITY`` when no item in the run called a tool.

    Without this the eval service rejects the metric ("requires tool calls in the
    evaluation trace, but no function_call/function_response events were found")
    and the harness quietly reports one metric fewer, giving no clue why. One
    tool-using item anywhere in the run is enough to score it, so this only fires
    at exactly zero.
    """
    if with_calls or not total:
        return metrics
    return [m for m in metrics if getattr(m, "name", str(m)) != "TOOL_USE_QUALITY"]


def _resolve_agent_resource_name(agent_id: str) -> str:
    if agent_id.startswith("projects/"):
        return agent_id
    return f"projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/reasoningEngines/{agent_id}"


# Which deployed engine serves each agent's eval cases, when no --agent-id is given.
# The router is its own deployment; everything else lives on the coordinator engine
# (travel/expense have their own engines only in a full deploy_agents all run, and
# AGENT_ENGINE_ID stays the safe default for them). Resolved per agent because a
# single run can span agents on different engines — scoring ROUTER_EVAL_CASES
# against a coordinator used to happen silently.
_DEFAULT_ENGINE_BY_AGENT = {"router_agent": ROUTER_ENGINE_ID}


def _engine_for_agent(agent_name: str, agent_id: str | None) -> str:
    """Resource name of the engine to evaluate ``agent_name`` against.

    An explicit ``agent_id`` wins for every agent — the bake-off deliberately
    pins one engine for the whole run. Otherwise each agent falls back to its own
    default deployment.
    """
    if agent_id:
        return _resolve_agent_resource_name(agent_id)
    return _resolve_agent_resource_name(_DEFAULT_ENGINE_BY_AGENT.get(agent_name, AGENT_ENGINE_ID))


def _annotate_low_confidence(metric_results: dict, n_items: int) -> dict:
    """Tag every metric ``low_confidence`` when graded over fewer than the floor.

    All metrics in a run share the same item count, so the flag is computed once
    from ``n_items`` (:data:`src.eval.stats.MIN_SAMPLES`). Mutates and returns
    ``metric_results`` for convenience.
    """
    from src.eval.stats import is_low_confidence

    low = is_low_confidence(n_items)
    for detail in metric_results.values():
        detail["low_confidence"] = low
    return metric_results


def _select_cases(agent_name: str, limit: int | None) -> list[dict]:
    """Eval cases for an agent, capped to ``limit`` (None = all).

    The CI eval gate passes a small ``limit`` to keep a run ~3-5 min; slicing an
    empty limit is a no-op so normal full runs are unaffected.
    """
    cases = get_eval_cases(agent_name)
    return cases[:limit] if limit else cases


def _build_eval_dataset(cases: list[dict]) -> pd.DataFrame:
    session_inputs = types.evals.SessionInput(user_id="eval-batch-user", state={})
    rows = []
    for case in cases:
        row = {
            "prompt": case["prompt"],
            "session_inputs": session_inputs,
            "eval_category": case["category"],
            "expected_tool": case["expected_tool"],
            "expected_signals": json.dumps(case["expected_signals"]),
            "case_description": case["description"],
        }
        if "reference" in case:
            row["reference"] = case["reference"]
        rows.append(row)
    return pd.DataFrame(rows)


def _run_single_agent_eval(
    client: Client,
    agent_name: str,
    agent_resource_name: str,
    score_threshold: float,
    limit: int | None = None,
) -> dict:
    """Run batch evaluation for a single agent."""
    cases = _select_cases(agent_name, limit)
    metrics = get_metrics(agent_name)

    print(f"\n{'─' * 60}")
    print(f"  Agent: {agent_name} ({len(cases)} test cases)")
    print(f"  Metrics: {', '.join(getattr(m, 'name', str(m)) for m in metrics)}")
    print(f"{'─' * 60}")

    eval_df = _build_eval_dataset(cases)

    # Warm the engine before the batched fan-out so cold-start empties don't
    # drop items (throttle + retry-on-empty in _sdk_patches cover the rest).
    try:
        engine = client.agent_engines.get(name=agent_resource_name)
        warmed = warm_agent_engine(engine)
        print(f"  Warmed engine ({warmed} warmup queries returned content)")
    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"  Warmup skipped: {e}")

    # Run inference
    print("  Running inference...")
    t0 = time.time()
    inference_result = client.evals.run_inference(
        agent=agent_resource_name,
        src=eval_df,
    )
    elapsed = time.time() - t0
    print(f"  Inference complete in {elapsed:.1f}s")

    # TOOL_USE_QUALITY is scored from the AgentData events, not the response text.
    # If nothing in the run called a tool the service rejects the metric and the
    # run silently comes back with one metric fewer — so say so, and say the count
    # even when it is fine so a low score is interpretable.
    with_calls, total_items = count_tool_call_items(
        getattr(inference_result, "eval_dataset_df", None)
    )
    print(f"  Tool calls: {with_calls}/{total_items} items invoked at least one tool")
    if total_items and not with_calls:
        print(
            "  tool_use_quality: SKIPPED — no item made a tool call, so the metric "
            "cannot be scored (it grades the trace, not the answer). "
            "See docs/notes/router-tool-use-quality.md"
        )
    metrics = drop_tool_use_metric_if_unscorable(metrics, with_calls, total_items)

    # Run evaluation
    print("  Running evaluation...")
    ensure_eval_experiment(client=client)
    evaluation_run = client.evals.create_evaluation_run(
        dataset=inference_result,
        agent=agent_resource_name,
        metrics=metrics,
        dest=GCS_EVAL_DEST,
        display_name=eval_run_display_name(agent_name, "batch"),
        labels=eval_run_labels(agent_name, "batch"),
    )

    print(f"  Eval run: {evaluation_run.name}")
    print("  Polling", end="", flush=True)
    poll_start = time.time()
    while time.time() - poll_start < MAX_POLL_SECONDS:
        evaluation_run = client.evals.get_evaluation_run(name=evaluation_run.name)
        state = str(getattr(evaluation_run, "state", ""))
        if "SUCCEEDED" in state or "FAILED" in state or "CANCELLED" in state:
            break
        print(".", end="", flush=True)
        time.sleep(15)
    print(f" {state}")

    if "FAILED" in state:
        err = getattr(evaluation_run, "error", None)
        print(f"  ERROR: {err}")
        return {
            "agent": agent_name,
            "status": "FAILED",
            "error": str(err),
            "test_cases": len(cases),
        }

    # Retrieve full results
    evaluation_run = client.evals.get_evaluation_run(
        name=evaluation_run.name,
        include_evaluation_items=True,
    )

    # Extract metrics — summary_metrics is a SummaryMetric pydantic model
    # with .metrics (dict) and .total_items attributes
    raw_metrics: dict = {}
    total_items = 0
    try:
        run_results = getattr(evaluation_run, "evaluation_run_results", None)
        if run_results:
            sm = getattr(run_results, "summary_metrics", None)
            if sm:
                total_items = getattr(sm, "total_items", 0) or 0
                nested = getattr(sm, "metrics", None)
                if nested:
                    raw_metrics = dict(nested) if not isinstance(nested, dict) else nested
    except Exception as e:
        print(f"  Warning: could not extract summary metrics: {e}")

    # Extract AVERAGE scores (keys like "agent_engine_0/safety_v1/AVERAGE")
    # API returns scores on 0-1 scale; normalize threshold accordingly
    normalized_threshold = score_threshold / 5.0
    metric_results = {}
    all_pass = True
    for key, value in raw_metrics.items():
        if "/AVERAGE" in key:
            avg = float(value)
            passed = avg >= normalized_threshold
            if not passed:
                all_pass = False
            metric_results[key.rsplit("/AVERAGE", 1)[0]] = {
                "score": avg,
                "threshold": normalized_threshold,
                "passed": passed,
            }
    # Flag every metric low-confidence when graded over too few items, so a
    # pass/fail over a demo-scale run isn't read with full trust.
    _annotate_low_confidence(metric_results, total_items)

    # Per-item details
    items = []
    try:
        if hasattr(evaluation_run, "evaluation_items"):
            for item in evaluation_run.evaluation_items or []:
                items.append(dict(item) if not isinstance(item, dict) else item)
    except Exception:
        pass

    # Print agent summary
    print(f"\n  Results for {agent_name} ({total_items} items):")
    for mname, detail in sorted(metric_results.items()):
        status = "PASS" if detail["passed"] else "FAIL"
        marker = "" if detail["passed"] else "  <<<"
        conf = "  ⚠ low_confidence" if detail.get("low_confidence") else ""
        print(
            f"    {mname:50s} {detail['score']:.2f} / {detail['threshold']:.2f}  "
            f"[{status}]{marker}{conf}"
        )
    if not metric_results:
        print(
            f"    (no metrics returned — check eval run: {getattr(evaluation_run, 'name', 'N/A')})"
        )

    return {
        "agent": agent_name,
        "status": "PASSED" if all_pass else "FAILED",
        "test_cases": len(cases),
        "inference_seconds": round(elapsed, 1),
        "metrics": metric_results,
        "summary_raw": raw_metrics,
        "evaluation_run_name": getattr(evaluation_run, "name", None),
        "item_count": len(items),
        "items": items,
    }


def run_multi_agent_batch_eval(
    agents: list[str] | None = None,
    agent_id: str | None = None,
    score_threshold: float = 3.0,
    output_path: str | None = None,
    limit: int | None = None,
) -> dict:
    """Run batch evaluations for multiple agents."""
    if agents is None:
        agents = ALL_AGENTS

    run_id = f"multi_agent_eval_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    # Resolved per agent, not once: the router is its own deployment, so a run
    # spanning agents can span engines. An explicit --agent-id still pins all of
    # them (the bake-off relies on that).
    engines = {name: _engine_for_agent(name, agent_id) for name in agents}

    print(f"{'=' * 60}")
    print("MULTI-AGENT BATCH EVALUATION")
    print(f"{'=' * 60}")
    print(f"  Run ID:    {run_id}")
    print(f"  Threshold: {score_threshold}")
    print(f"  Agents:    {len(agents)}")
    for name in agents:
        print(f"    {name:<20} -> {engines[name].split('/')[-1]}")

    # Initialize
    vertexai.init(
        project=GCP_PROJECT_ID,
        location=GCP_REGION,
        staging_bucket=f"gs://{GCP_STAGING_BUCKET}",
    )
    client = Client(project=GCP_PROJECT_ID, location=GCP_REGION)

    # Run evals per agent
    agent_results = {}
    for agent_name in agents:
        try:
            result = _run_single_agent_eval(
                client=client,
                agent_name=agent_name,
                agent_resource_name=engines[agent_name],
                score_threshold=score_threshold,
                limit=limit,
            )
            agent_results[agent_name] = result
        except Exception as e:
            print(f"\n  ERROR evaluating {agent_name}: {e}")
            agent_results[agent_name] = {
                "agent": agent_name,
                "status": "ERROR",
                "error": str(e),
            }

    # Cross-agent summary
    total_cases = sum(int(r.get("test_cases", 0)) for r in agent_results.values())
    agents_passed = sum(1 for r in agent_results.values() if r.get("status") == "PASSED")
    all_passed = agents_passed == len(agents)

    results = {
        "run_id": run_id,
        "timestamp": datetime.now().isoformat(),
        # Kept for back-compat with readers that expect one engine per run
        # (harvest / publish_offline_eval); ``agent_engines`` is the honest map
        # now that a run can span deployments.
        "agent_engine": next(iter(engines.values()), None),
        "agent_engines": engines,
        "score_threshold": score_threshold,
        "total_agents": len(agents),
        "agents_passed": agents_passed,
        "all_passed": all_passed,
        "total_test_cases": total_cases,
        "agents": agent_results,
    }

    # Print overall summary
    print(f"\n{'=' * 60}")
    print("OVERALL RESULTS")
    print(f"{'=' * 60}")
    for name, r in agent_results.items():
        status = r.get("status", "UNKNOWN")
        cases = r.get("test_cases", 0)
        metrics_count = len(r.get("metrics", {}))
        print(f"  {name:25s} {status:8s}  ({cases} cases, {metrics_count} metrics)")
    print(f"\n  Overall: {'PASS' if all_passed else 'FAIL'} ({agents_passed}/{len(agents)} agents)")
    print(f"{'=' * 60}")

    # Save results
    output_dir = Path(EVAL_OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    if output_path is None:
        output_path = str(output_dir / f"batch_results_{run_id}.json")

    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to: {output_path}")

    return results


def list_all_cases():
    """Print all test cases organized by agent."""
    for agent_name in ALL_AGENTS:
        cases = get_eval_cases(agent_name)
        print(f"\n{'═' * 60}")
        print(f" {agent_name} ({len(cases)} test cases)")
        print(f"{'═' * 60}")
        for i, case in enumerate(cases, 1):
            print(f"  [{i:2d}] {case['category']:25s} | {case['prompt'][:70]}")
            print(f"       Tool: {case['expected_tool']}  Signals: {case['expected_signals']}")
            if "expected_complexity" in case:
                print(f"       Complexity: {case['expected_complexity']}")


def main():
    parser = argparse.ArgumentParser(
        description="Run batch evaluations across multiple agents.",
    )
    parser.add_argument(
        "--agents",
        type=str,
        default=None,
        help=f"Comma-separated agent names. Default: all ({','.join(ALL_AGENTS)})",
    )
    parser.add_argument(
        "--agent-id",
        default=None,
        help=(
            "Pin every agent to this Agent Engine ID. Default: per agent — "
            f"router_agent -> {ROUTER_ENGINE_ID}, others -> {AGENT_ENGINE_ID}."
        ),
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=3.0,
        help="Minimum score to pass (1-5). Default: 3.0",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output JSON file path.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap cases per agent (e.g. 8 for a fast CI gate). Default: all cases.",
    )
    parser.add_argument(
        "--list-cases",
        action="store_true",
        help="Print all test cases and exit.",
    )
    args = parser.parse_args()

    if args.list_cases:
        list_all_cases()
        return

    agents = args.agents.split(",") if args.agents else None

    results = run_multi_agent_batch_eval(
        agents=agents,
        agent_id=args.agent_id,
        score_threshold=args.threshold,
        output_path=args.output,
        limit=args.limit,
    )

    if not results["all_passed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
