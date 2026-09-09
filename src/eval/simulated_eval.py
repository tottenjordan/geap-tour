"""Simulated evaluation — generate synthetic scenarios and run agent inference for CI/CD.

Supports per-agent evaluation with conversation scenarios,
ADK user simulator with configurable max turns, and multi-turn metrics.

Usage:
    uv run python -m src.eval.simulated_eval --agent-id <AGENT_ENGINE_ID> --agent-name coordinator_agent
    uv run python -m src.eval.simulated_eval --agent-id <ROUTER_ENGINE_ID> --agent-name router_agent --scenario-count 10
"""


def _patch_evals_extra_fields():
    """Let ``ConversationTurn`` keep the fields the API actually returns.

    The API returns turn data carrying ADK **Event** fields — ``content``,
    ``author``, ``actions``, ``invocation_id``, ``id``, ``timestamp``,
    ``usage_metadata`` — that the SDK's ``ConversationTurn`` does not declare. The
    base class is ``extra='forbid'``, so parsing raises ``ValidationError``.

    **``extra='allow'``, NOT ``'ignore'``.** That one word is the whole fix. This
    patch used to say ``'ignore'``, which stops the exception by **discarding every
    unrecognised field** — and on this SDK those fields *are* the conversation. The
    result was a green run over nothing:

        extra='ignore'  ->  1 turn,  every field dropped, events=[]  -> metrics {}
        extra='allow'   ->  3 turns, content/author/actions preserved

    Measured 2026-09-09 against aiplatform 2.1.0. The eval run reported
    ``SUCCEEDED`` with ``total_items=2, failed_items=None`` and
    ``metric_results: {}`` for every case — the multi-turn raters correctly scored
    nothing, because nothing survived parsing. `simulated_eval` then printed
    "(no metrics returned)" and still wrote ``all_passed: true``.

    So the failure mode this patch was written to prevent (a loud ValidationError)
    was replaced by a silent one. Prefer keeping data you do not understand over
    dropping it: an unexpected extra field is a much smaller problem than an empty
    conversation that scores as a pass.

    Applied module-wide rather than to a named list, mirroring how
    ``src/optimize/run_optimize.py:_patch_adk`` handles the same problem for ADK.
    Naming classes individually is how this stayed broken: the first fix relaxed
    ``ConversationTurn`` and ``AgentData``, and the very next layer down —
    ``AgentEvent``, which declares only 5 of the ~12 fields the API sends — threw
    the identical error one level deeper.

    Upstream: https://github.com/googleapis/python-aiplatform/issues/6785
    """
    import importlib

    import pydantic

    # BOTH copies. `agentplatform._genai` and `vertexai._genai` are separate module
    # objects with separate classes (`agentplatform.types is vertexai.types` -> False,
    # see docs/notes/agentplatform-client-migration.md), and patching only one is a
    # silent no-op — the same trap that made `_sdk_patches` collapse every metric to
    # ~0. Verified 2026-09-09: both declare ConversationTurn as extra='forbid'.
    modules = []
    for path in ("agentplatform._genai.types.evals", "vertexai._genai.types.evals"):
        try:
            modules.append(importlib.import_module(path))
        except ImportError:  # pragma: no cover - one copy may not ship forever
            continue

    for evals_types in modules:
        for name in dir(evals_types):
            cls = getattr(evals_types, name, None)
            if (
                isinstance(cls, type)
                and issubclass(cls, pydantic.BaseModel)
                and cls.model_config.get("extra") == "forbid"
            ):
                cls.model_config["extra"] = "allow"
                cls.__pydantic_complete__ = False
                cls.model_rebuild(force=True)


def regroup_events_into_turns(agent_data: dict | None) -> dict | None:
    """Reshape flat ADK events into the ``ConversationTurn`` structure raters read.

    ``ConversationTurn`` is declared as ``{turn_index, turn_id, events[]}``, but on
    aiplatform 2.1.0 ``run_inference`` returns each ADK **Event** as a top-level
    entry in ``turns`` — ``author``/``content``/``actions``/``invocation_id`` sit
    directly on the turn and ``events`` is empty. The multi-turn raters read
    ``turn.events``, find nothing, and return **no metrics at all** while the
    evaluation run still reports ``SUCCEEDED``.

    Grouping key is **``invocation_id``**, not position: one invocation is one
    user-visible turn (a user message plus the agent's events answering it), which
    is exactly the unit "multi-turn" means. Order is preserved, and each group gets
    the ``turn_index`` the raters expect. Events with no ``invocation_id`` fall back
    to one-event-per-turn rather than being merged into a neighbour, because a
    wrong grouping produces plausible-looking multi-turn scores, which is worse than
    a conservative one.

    Already-correct data passes through untouched: a turn that has a non-empty
    ``events`` list is left exactly as-is, so this is a no-op the moment the SDK or
    service starts returning the declared shape.
    """
    if not isinstance(agent_data, dict):
        return agent_data
    turns = agent_data.get("turns")
    if not turns:
        return agent_data
    # Already the declared shape — do not touch it.
    if any((t or {}).get("events") for t in turns):
        return agent_data

    grouped: list[dict] = []
    current_key = object()  # sentinel: never equal to a real invocation_id
    for event in turns:
        if not isinstance(event, dict):
            continue
        key = event.get("invocation_id")
        if key is None or key != current_key:
            grouped.append({"turn_index": len(grouped), "turn_id": key, "events": []})
            current_key = key if key is not None else object()
        grouped[-1]["events"].append(event)

    return {**agent_data, "turns": grouped}


def regroup_dataset_turns(inference_result):
    """Apply :func:`regroup_events_into_turns` to every row's ``agent_data``."""
    df = getattr(inference_result, "eval_dataset_df", None)
    if df is None or "agent_data" not in getattr(df, "columns", []):
        return inference_result
    df["agent_data"] = [regroup_events_into_turns(cell) for cell in df["agent_data"]]
    return inference_result


GENERATION_INSTRUCTIONS = {
    "coordinator_agent": (
        "Generate diverse scenarios covering: flight search, hotel booking, "
        "expense submission within policy, over-limit expenses, booking cancellation, "
        "and multi-step travel planning with expense management."
    ),
    "travel_agent": (
        "Generate diverse scenarios covering: flight search by route and date, "
        "hotel search with price filters, booking confirmation flows, "
        "comparison shopping between options, and edge cases with invalid airports."
    ),
    "expense_agent": (
        "Generate diverse scenarios covering: expense policy checks for all categories, "
        "within-limit and over-limit submissions, expense history review, "
        "invalid category handling, and multi-expense submission flows."
    ),
    "router_agent": (
        "Generate scenarios with varying complexity levels: "
        "simple single-intent lookups (low complexity), moderate reasoning and "
        "multi-step queries (medium complexity), and complex cross-domain "
        "planning tasks requiring deep analysis (high complexity)."
    ),
}


def run_simulated_eval(
    agent_resource_name: str,
    agent_name: str = "coordinator_agent",
    scenario_count: int = 10,
    max_turns: int = 5,
    score_threshold: float = 3.0,
) -> bool:
    """Run simulated evaluation. Returns True if all metrics pass threshold."""
    _patch_evals_extra_fields()

    import vertexai
    from agentplatform import Client

    from src.config import GCP_PROJECT_ID, GCP_REGION, SIMULATOR_MODEL
    from src.eval.agent_eval_configs import build_agent_info, get_multi_turn_metrics

    vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
    client = Client(project=GCP_PROJECT_ID, location=GCP_REGION)
    from src.config import disable_pyopenssl

    disable_pyopenssl()

    # Multi-turn task success + tool-use + trajectory quality
    # (see agent_eval_configs.get_multi_turn_metrics).
    eval_metrics = get_multi_turn_metrics()

    agent_info = build_agent_info(agent_name)

    generation_instruction = GENERATION_INSTRUCTIONS.get(
        agent_name, GENERATION_INSTRUCTIONS["coordinator_agent"]
    )

    print(f"[1/3] Generating {scenario_count} conversation scenarios for {agent_name}...")
    eval_dataset = client.evals.generate_conversation_scenarios(
        agent_info=agent_info,
        config={
            "count": scenario_count,
            "generation_instruction": generation_instruction,
        },
        allow_cross_region_model=True,
    )
    print("  Generated scenarios")

    print(f"[2/3] Running inference (max {max_turns} turns per scenario)...")
    eval_dataset_with_traces = client.evals.run_inference(
        agent=agent_resource_name,
        src=eval_dataset,
        config={
            "user_simulator_config": {
                "max_turn": max_turns,
                "model_name": SIMULATOR_MODEL,
            },
        },
    )
    # The raters read turn.events; on aiplatform 2.1.0 run_inference returns flat
    # ADK events as turns with events=[]. Regroup before scoring, or every metric
    # comes back empty while the run still reports SUCCEEDED.
    eval_dataset_with_traces = regroup_dataset_turns(eval_dataset_with_traces)
    print("  Inference complete")

    import time

    from src.config import GCP_STAGING_BUCKET
    from src.eval.eval_experiment import (
        ensure_eval_experiment,
        eval_run_display_name,
        eval_run_labels,
    )

    GCS_EVAL_DEST = f"gs://{GCP_STAGING_BUCKET}/eval-results/"
    MAX_POLL_SECONDS = 600

    print("[3/3] Creating evaluation run...")
    ensure_eval_experiment(client=client)
    evaluation_run = client.evals.create_evaluation_run(
        dataset=eval_dataset_with_traces,
        agent=agent_resource_name,
        metrics=eval_metrics,
        dest=GCS_EVAL_DEST,
        display_name=eval_run_display_name(agent_name, "simulated"),
        labels=eval_run_labels(agent_name, "simulated"),
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
        return False

    evaluation_run = client.evals.get_evaluation_run(
        name=evaluation_run.name,
        include_evaluation_items=True,
    )

    raw_metrics: dict = {}
    try:
        run_results = getattr(evaluation_run, "evaluation_run_results", None)
        if run_results:
            sm = getattr(run_results, "summary_metrics", None)
            if sm:
                nested = getattr(sm, "metrics", None)
                if nested:
                    raw_metrics = dict(nested) if not isinstance(nested, dict) else nested
    except Exception as e:
        print(f"  Warning: could not extract summary metrics: {e}")

    normalized_threshold = score_threshold / 5.0
    all_pass = True
    metric_results = {}

    print(f"\n=== Simulated Evaluation Results ({agent_name}) ===")
    for key, value in sorted(raw_metrics.items()):
        if "/AVERAGE" in key:
            avg = float(value)
            passed = avg >= normalized_threshold
            if not passed:
                all_pass = False
            status = "PASS" if passed else "FAIL"
            metric_name = key.rsplit("/AVERAGE", 1)[0]
            metric_results[metric_name] = {
                "score": avg,
                "threshold": normalized_threshold,
                "passed": passed,
            }
            print(f"  {metric_name:50s} {avg:.2f} / {normalized_threshold:.2f}  [{status}]")

    if not metric_results:
        # A run that scored NOTHING must not report success. `all_pass` starts True
        # and only flips on a failing metric, so zero metrics used to sail through
        # as `all_passed: true` — the exact green-over-nothing this repo keeps
        # finding, and how the extra='ignore' data loss above stayed invisible.
        # An empty result is an INFRA outcome, not a quality verdict.
        all_pass = False
        print("  NO METRICS RETURNED — this run measured nothing, so it is a FAIL.")
        print("    The eval run itself may report SUCCEEDED: the service scores what")
        print("    it is given, and an empty conversation scores as no metrics.")
        print("    Check agent_data.turns[].events before suspecting the agent.")
        print(f"  Eval run: {getattr(evaluation_run, 'name', 'N/A')}")

    import json
    from datetime import datetime
    from pathlib import Path

    from src.config import EVAL_OUTPUT_DIR

    results = {
        "type": "simulated_eval",
        "agent_name": agent_name,
        "agent_engine": agent_resource_name,
        "evaluation_run": getattr(evaluation_run, "name", None),
        "timestamp": datetime.now().isoformat(),
        "scenario_count": scenario_count,
        "max_turns": max_turns,
        "score_threshold": score_threshold,
        "all_passed": all_pass,
        "metrics": metric_results,
    }

    output_dir = Path(EVAL_OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = (
        output_dir / f"simulated_eval_{agent_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to: {output_path}")

    return all_pass


if __name__ == "__main__":
    import argparse
    import sys

    from src.config import AGENT_ENGINE_ID

    parser = argparse.ArgumentParser(
        description="Run simulated multi-turn evaluation with LLM-backed user simulator.",
    )
    parser.add_argument(
        "--agent-id",
        default=AGENT_ENGINE_ID,
        help=f"Agent Engine ID or full resource name. Default: {AGENT_ENGINE_ID}",
    )
    parser.add_argument(
        "--agent-name",
        default="coordinator_agent",
        help="Agent config to use (coordinator_agent, travel_agent, expense_agent, router_agent). Default: coordinator_agent",
    )
    parser.add_argument(
        "--scenario-count",
        type=int,
        default=5,
        help="Number of conversation scenarios to generate. Default: 5",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=5,
        help="Max conversation turns per scenario. Default: 5",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=3.0,
        help="Minimum score to pass (1-5). Default: 3.0",
    )
    args = parser.parse_args()

    resource = args.agent_id
    if not resource.startswith("projects/"):
        from src.config import GCP_PROJECT_ID, GCP_REGION

        resource = f"projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/reasoningEngines/{resource}"

    passed = run_simulated_eval(
        resource,
        agent_name=args.agent_name,
        scenario_count=args.scenario_count,
        max_turns=args.max_turns,
        score_threshold=args.threshold,
    )
    sys.exit(0 if passed else 1)
