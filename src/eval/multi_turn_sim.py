"""Multi-turn simulation that actually drives the DEPLOYED engine.

``simulated_eval`` cannot do this, and not for a reason we control:
``agentplatform._genai._evals_common._run_agent`` passes
``user_simulator_config=None`` whenever it is handed a ``runtime`` (a deployed
engine), forwarding it only for a local in-process ``LlmAgent``. So ``--max-turns``
never leaves the client, every run is single-turn, and the three ``multi_turn_*``
raters grade one turn — two of them scoring ``0.00`` by construction. Measured
2026-09-17 on google-cloud-aiplatform 2.1.0; pinned by
``tests/test_simulated_eval.py:TestTheSdkDiscardsTheSimulatorOnTheRuntimePath``.

The other supported option is the local-``LlmAgent`` path, which is genuinely
multi-turn but measures **local code**. This module takes the other branch: keep
measuring the real deployed engine, and own the conversation loop.

How it works — one session per scenario, which is the whole point:

1. ``create_session`` once, then send ``starting_prompt``.
2. Read the agent's turn off the stream.
3. Ask a simulator model, given ``conversation_plan`` + the transcript so far, for
   the **next user utterance** — or ``[END]`` when the plan is exhausted.
4. Repeat into the *same* session until ``[END]``, ``max_turns``, or an empty
   response.

Reuses rather than reinvents: :mod:`src.eval.raw_stream` for the transport (the
SDK's ``stream_query`` raises "Can only parse array of JSON objects" against a
healthy engine — see ``docs/notes``/memory on the SSE-parse skew),
:func:`src.eval.judge_client.build_judge_generate_fn` for a deterministic,
retrying simulator, and :func:`src.traffic.generate_traffic._extract_text` for
visible text.

The emitted ``agent_data`` is the shape the managed raters read —
``{"turns": [{"turn_index", "turn_id", "events": [...]}]}`` — and **includes the
user turns**, which the SDK path never produced (a captured raw response had zero
``user``-authored events). With ``--score`` the conversations are handed to the same
``MULTI_TURN_*`` rubric metrics, so the number is comparable to what
``simulated_eval`` was trying to produce.

Cost is linear and deliberate: ``scenarios x max_turns`` engine calls plus
``scenarios x (max_turns - 1)`` simulator calls. Defaults are small; raise
knowingly.

Usage::

    uv run python -m src.eval.multi_turn_sim --agent-id <ENGINE_ID> --dry-run
    uv run python -m src.eval.multi_turn_sim --agent-id <ENGINE_ID> --scenario-count 2 --max-turns 4
    uv run python -m src.eval.multi_turn_sim --agent-id <ENGINE_ID> --score --threshold 3.0
"""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING, Any

from src.eval.types import (
    ConversationSummary,
    ConversationTurn,
    DiscriminationResult,
    MultiTurnScore,
    SimulatedConversation,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

#: The simulator says this when the conversation_plan is exhausted.
END_SENTINEL = "[END]"

DEFAULT_MAX_TURNS = 4
DEFAULT_SCENARIOS = 2
DEFAULT_USER_ID = "multi-turn-sim"

SIMULATOR_INSTRUCTIONS = """\
You are role-playing a HUMAN USER talking to a corporate travel-and-expense agent.

Follow this plan for how the conversation should go:
--- PLAN ---
{plan}
--- END PLAN ---

Conversation so far:
--- TRANSCRIPT ---
{transcript}
--- END TRANSCRIPT ---

Write the USER's next message, following the next unfinished step of the plan and
responding to what the agent just said.

Rules:
- Output ONLY the user's message. No quotes, no preamble, no stage directions.
- Stay in character as the user. Never answer as the agent.
- If every step of the plan is done, or the agent has nothing left to do, output
  exactly {end}
"""


def build_simulator_prompt(conversation_plan: str, transcript: Sequence[tuple[str, str]]) -> str:
    """Prompt asking the simulator for the next user utterance.

    The plan is passed verbatim: it is the generator's own description of the
    conversation, and paraphrasing it would quietly change what is being tested.
    """
    rendered = "\n".join(f"{speaker.upper()}: {text}".strip() for speaker, text in transcript)
    return SIMULATOR_INSTRUCTIONS.format(
        plan=conversation_plan.strip() or "(no plan provided — improvise plausibly)",
        transcript=rendered or "(nothing yet)",
        end=END_SENTINEL,
    )


def parse_simulator_reply(text: str | None) -> str | None:
    """The next user message, or ``None`` when the simulator says it is finished.

    Tolerant on purpose. A simulator that wraps its line in quotes, prefixes
    ``USER:``, or pads ``[END]`` with punctuation is behaving normally; treating any
    of those as a literal user utterance would inject garbage into the conversation
    and the raters would score it as the user being incoherent.
    """
    if text is None:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    # Strip a leading speaker label the model may add despite instructions.
    for label in ("USER:", "User:", "user:"):
        if cleaned.startswith(label):
            cleaned = cleaned[len(label) :].strip()
    if cleaned[:1] in {'"', "'"} and cleaned[-1:] == cleaned[:1] and len(cleaned) > 1:
        cleaned = cleaned[1:-1].strip()
    # `[END]`, `END`, `[END].` — anything whose alphanumerics are just "end".
    if "".join(ch for ch in cleaned if ch.isalnum()).upper() == "END":
        return None
    return cleaned or None


def build_turn(
    turn_index: int, user_message: str, agent_events: Sequence[dict]
) -> ConversationTurn:
    """One ``ConversationTurn``: the user's message followed by the agent's events.

    The user event is synthesized because the engine's stream only returns the
    agent's side. Including it is the point — a captured SDK-path response had
    **zero** ``user``-authored events, so the raters were grading a monologue.

    ``turn_id`` borrows the agent's ``invocation_id`` when present: one invocation is
    one user-visible turn, the same key
    ``simulated_eval.regroup_events_into_turns`` groups on.
    """
    invocation = next(
        (
            e.get("invocation_id")
            for e in agent_events
            if isinstance(e, dict) and e.get("invocation_id")
        ),
        None,
    )
    user_event = {
        "content": {"parts": [{"text": user_message}], "role": "user"},
        "author": "user",
        "invocation_id": invocation,
    }
    return {
        "turn_index": turn_index,
        "turn_id": invocation or f"turn-{turn_index}",
        "events": [user_event, *agent_events],
    }


def simulate_conversation(
    resource_name: str,
    starting_prompt: str,
    conversation_plan: str,
    *,
    simulate_fn: Callable[[str], str],
    max_turns: int = DEFAULT_MAX_TURNS,
    user_id: str = DEFAULT_USER_ID,
    create_session_fn: Callable[..., str] | None = None,
    stream_fn: Callable[..., list[dict]] | None = None,
    token: str | None = None,
) -> SimulatedConversation:
    """Drive one scenario to completion against the deployed engine.

    Returns ``{"turns", "transcript", "stopped", "turn_count", "tool_calls"}``.
    ``stopped`` is one of ``plan_complete`` / ``max_turns`` / ``empty_response``, and
    it is reported rather than hidden: a conversation that ended because the engine
    returned zero characters is an **infra** outcome, not a short conversation, and
    the two must never average together (the same separation
    ``online_monitor.infra_empty_rate`` makes).

    Every I/O dependency is injectable so the loop is testable with no engine.
    """
    from src.eval import raw_stream
    from src.traffic.generate_traffic import _extract_text

    create_session_fn = create_session_fn or raw_stream.create_session
    stream_fn = stream_fn or raw_stream.stream_query_events

    session_id = create_session_fn(resource_name, user_id, token=token)

    turns: list[ConversationTurn] = []
    transcript: list[tuple[str, str]] = []
    tool_calls: list[str] = []
    user_message = starting_prompt
    stopped = "max_turns"

    for index in range(max_turns):
        events = stream_fn(
            resource_name,
            message=user_message,
            user_id=user_id,
            session_id=session_id,
            token=token,
        )
        agent_text = "".join(_extract_text(e) for e in events)
        tool_calls.extend(_tool_names(events))
        turns.append(build_turn(index, user_message, events))
        transcript.append(("user", user_message))
        transcript.append(("agent", agent_text))

        if not agent_text.strip():
            # Stop driving a conversation the engine has stopped answering; a
            # simulator replying to silence produces a transcript that looks like a
            # quality problem instead of the infra failure it is.
            stopped = "empty_response"
            break
        if index == max_turns - 1:
            break

        nxt = parse_simulator_reply(
            simulate_fn(build_simulator_prompt(conversation_plan, transcript))
        )
        if nxt is None:
            stopped = "plan_complete"
            break
        user_message = nxt

    return {
        "turns": turns,
        "transcript": transcript,
        "stopped": stopped,
        "turn_count": len(turns),
        "tool_calls": tool_calls,
    }


def _tool_names(events: Sequence[dict]) -> list[str]:
    """Names of tools actually invoked in these events."""
    names = []
    for event in events:
        if not isinstance(event, dict):
            continue
        for part in (event.get("content") or {}).get("parts") or []:
            call = part.get("function_call") if isinstance(part, dict) else None
            if call and call.get("name"):
                names.append(call["name"])
    return names


def conversations_to_dataframe(
    scenarios: Sequence[dict], conversations: Sequence[SimulatedConversation]
):
    """Assemble the rater-facing dataset: one row per conversation.

    Mirrors the columns ``run_inference`` produces on the simulated path
    (``starting_prompt`` / ``conversation_plan`` / ``agent_data``) so the managed
    ``MULTI_TURN_*`` metrics consume it unchanged.
    """
    import pandas as pd

    return pd.DataFrame(
        {
            "starting_prompt": [s.get("starting_prompt", "") for s in scenarios],
            "conversation_plan": [s.get("conversation_plan", "") for s in scenarios],
            "agent_data": [{"agents": None, "turns": c["turns"]} for c in conversations],
        }
    )


def summarize(conversations: Sequence[SimulatedConversation]) -> ConversationSummary:
    """Shape of what we produced — the thing the SDK path could never show."""
    counts = [c["turn_count"] for c in conversations]
    return {
        "conversations": len(conversations),
        "turns_total": sum(counts),
        "turns_mean": round(sum(counts) / len(counts), 2) if counts else 0.0,
        "turns_max": max(counts, default=0),
        "multi_turn_conversations": sum(1 for n in counts if n > 1),
        "empty_response_conversations": sum(
            1 for c in conversations if c["stopped"] == "empty_response"
        ),
        "stopped_reasons": {
            reason: sum(1 for c in conversations if c["stopped"] == reason)
            for reason in sorted({c["stopped"] for c in conversations})
        },
        "tool_calls": sorted({name for c in conversations for name in c["tool_calls"]}),
    }


def render(summary: dict[str, Any]) -> str:
    lines = [
        f"  conversations:        {summary['conversations']}",
        f"  turns (mean/max):     {summary['turns_mean']} / {summary['turns_max']}",
        f"  genuinely multi-turn: {summary['multi_turn_conversations']}/{summary['conversations']}",
        f"  stopped because:      {summary['stopped_reasons']}",
        f"  tools exercised:      {', '.join(summary['tool_calls']) or '(none)'}",
    ]
    if summary["empty_response_conversations"]:
        lines.append(
            f"  WARNING: {summary['empty_response_conversations']} conversation(s) ended on an "
            "EMPTY response — an infra failure, not a short conversation."
        )
    return "\n".join(lines)


def _build_simulator(model: str | None = None) -> Callable[[str], str]:
    from src.config import SIMULATOR_MODEL
    from src.eval.judge_client import build_judge_generate_fn

    return build_judge_generate_fn(model or SIMULATOR_MODEL)


def run_multi_turn_sim(
    agent_id: str,
    *,
    agent_name: str = "coordinator_agent",
    scenario_count: int = DEFAULT_SCENARIOS,
    max_turns: int = DEFAULT_MAX_TURNS,
    score: bool = False,
    score_threshold: float = 3.0,
    simulate_fn: Callable[[str], str] | None = None,
    scenarios: Sequence[dict] | None = None,
) -> dict[str, Any]:
    """Generate scenarios, drive them multi-turn, optionally score them."""
    import vertexai

    from src.config import GCP_PROJECT_ID, GCP_REGION, disable_pyopenssl
    from src.eval.batch_eval import _resolve_agent_resource_name

    vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
    disable_pyopenssl()
    resource_name = _resolve_agent_resource_name(agent_id)

    if scenarios is None:
        scenarios = _generate_scenarios(agent_name, scenario_count)
    simulate_fn = simulate_fn or _build_simulator()

    conversations = []
    for i, scenario in enumerate(scenarios, start=1):
        print(f"  [{i}/{len(scenarios)}] {scenario.get('starting_prompt', '')[:78]}")
        convo = simulate_conversation(
            resource_name,
            scenario.get("starting_prompt", ""),
            scenario.get("conversation_plan", ""),
            simulate_fn=simulate_fn,
            max_turns=max_turns,
        )
        print(f"        -> {convo['turn_count']} turns ({convo['stopped']})")
        conversations.append(convo)

    result: dict[str, Any] = {
        "_scenarios": list(scenarios),
        "agent_id": agent_id,
        "agent_name": agent_name,
        "max_turns": max_turns,
        "summary": summarize(conversations),
        "conversations": conversations,
    }
    if score:
        result["scores"] = _score(
            scenarios, conversations, resource_name, agent_name, score_threshold
        )
    return result


def _generate_scenarios(agent_name: str, count: int) -> list[dict]:
    """Reuse the same scenario generator ``simulated_eval`` uses."""
    from agentplatform import Client

    from src.config import GCP_PROJECT_ID, GCP_REGION
    from src.eval.agent_eval_configs import build_agent_info
    from src.eval.simulated_eval import GENERATION_INSTRUCTIONS, _patch_evals_extra_fields

    _patch_evals_extra_fields()
    client = Client(project=GCP_PROJECT_ID, location=GCP_REGION)
    dataset = client.evals.generate_conversation_scenarios(
        agent_info=build_agent_info(agent_name),
        config={
            "count": count,
            "generation_instruction": GENERATION_INSTRUCTIONS.get(
                agent_name, GENERATION_INSTRUCTIONS["coordinator_agent"]
            ),
        },
        allow_cross_region_model=True,
    )
    df = getattr(dataset, "eval_dataset_df", dataset)
    return df.to_dict("records")


def _score(
    scenarios: Sequence[dict],
    conversations: Sequence[SimulatedConversation],
    resource_name: str,
    agent_name: str,
    threshold: float,
) -> MultiTurnScore:
    """Hand the real multi-turn conversations to the managed MULTI_TURN_* raters."""
    import time

    from agentplatform import Client, types

    from src.config import GCP_PROJECT_ID, GCP_REGION, GCP_STAGING_BUCKET
    from src.eval.agent_eval_configs import get_multi_turn_metrics
    from src.eval.eval_experiment import (
        ensure_eval_experiment,
        eval_run_display_name,
        eval_run_labels,
    )
    from src.eval.simulated_eval import _patch_evals_extra_fields

    _patch_evals_extra_fields()
    client = Client(project=GCP_PROJECT_ID, location=GCP_REGION)

    # PARTITION INFRA OUT BEFORE SCORING. A conversation that ended because the
    # engine returned zero characters has nothing for a rubric to grade, and the
    # raters grade it anyway — so one dead stream drags the whole run's mean down
    # and an INFRA failure is published as a QUALITY score. Exactly what
    # `multi_agent_batch_eval.partition_empty_responses` and
    # `online_monitor.infra_empty_rate` exist to stop, missed here when this module
    # shipped: it labels `stopped=empty_response` correctly and then scored it.
    #
    # Found by the discrimination run, whose baseline came back 0.25/1.00/0.17
    # against 1.00/1.00/1.00 a day earlier — one of two conversations was a dead
    # stream.
    scenarios, conversations, n_empty = partition_empty_conversations(scenarios, conversations)
    if n_empty:
        print(
            f"  {n_empty}/{len(conversations)} conversation(s) ended EMPTY — excluded from scoring (infra, not quality)"
        )
    if not conversations:
        # Scoring nothing would publish a mean over zero items, which renders as
        # catastrophic quality. Say what happened instead.
        return {
            "state": "SKIPPED",
            "reason": "every conversation ended on an empty stream (infra failure)",
            "threshold": threshold / 5.0,
            "metrics": {},
            "empty_conversations": n_empty,
            "all_passed": False,
        }

    dataset = types.EvaluationDataset(
        eval_dataset_df=conversations_to_dataframe(scenarios, conversations)
    )

    ensure_eval_experiment(client=client)
    run = client.evals.create_evaluation_run(
        dataset=dataset,
        agent=resource_name,
        metrics=get_multi_turn_metrics(),
        dest=f"gs://{GCP_STAGING_BUCKET}/eval-results/",
        display_name=eval_run_display_name(agent_name, "multiturn"),
        labels=eval_run_labels(agent_name, "multiturn"),
    )
    print(f"  Eval run: {run.name}\n  Polling", end="", flush=True)
    state = ""
    deadline = time.time() + 900
    while time.time() < deadline:
        run = client.evals.get_evaluation_run(name=run.name)
        state = str(getattr(run, "state", ""))
        if any(s in state for s in ("SUCCEEDED", "FAILED", "CANCELLED")):
            break
        print(".", end="", flush=True)
        time.sleep(15)
    print(f" {state}")

    metrics: dict[str, float] = {}
    results = getattr(run, "evaluation_run_results", None)
    summary = getattr(results, "summary_metrics", None) if results else None
    nested = getattr(summary, "metrics", None) if summary else None
    for key, value in dict(nested or {}).items():
        if "/AVERAGE" in key:
            metrics[key.replace("/AVERAGE", "")] = float(value)

    floor = threshold / 5.0
    return {
        "state": state,
        "threshold": floor,
        "metrics": metrics,
        "empty_conversations": n_empty,
        "scored_conversations": len(conversations),
        # No metrics is NOT a pass — the failure mode PR #138 had to fix.
        "all_passed": bool(metrics) and all(v >= floor for v in metrics.values()),
    }


def partition_empty_conversations(
    scenarios: Sequence[dict], conversations: Sequence[SimulatedConversation]
) -> tuple[list[dict], list[SimulatedConversation], int]:
    """Drop conversations that died on an empty stream. Returns ``(scenarios, convos, n_empty)``.

    A conversation that ended because the engine returned zero characters has
    nothing for a rubric to grade — and the raters grade it anyway, so one dead
    stream drags the run's mean down and an INFRA failure is published as a QUALITY
    score. The same separation ``multi_agent_batch_eval.partition_empty_responses``
    and ``online_monitor.infra_empty_rate`` make.

    This module labelled ``stopped=empty_response`` correctly from the start and
    then scored it anyway. Found by a discrimination run whose baseline came back
    0.25/1.00/0.17 against 1.00/1.00/1.00 the day before — and which, worse, made
    ``trajectory_quality`` look BLIND to a defect it catches at -0.83, because an
    already-floored metric cannot fall further.
    """
    pairs = [
        (sc, cv)
        for sc, cv in zip(scenarios, conversations, strict=False)
        if cv.get("stopped") != "empty_response"
    ]
    return [sc for sc, _ in pairs], [cv for _, cv in pairs], len(conversations) - len(pairs)


def run_discrimination(
    agent_id: str,
    *,
    agent_name: str = "coordinator_agent",
    scenario_count: int = DEFAULT_SCENARIOS,
    max_turns: int = DEFAULT_MAX_TURNS,
    score_threshold: float = 3.0,
) -> DiscriminationResult:
    """Score real conversations against deliberately broken copies of them.

    The question this answers is not "how good is the agent" but "can this rubric
    tell good from bad at all". A first live run scored 1.00/1.00/1.00, which is
    consistent with a working metric AND with a metric that returns 1.00 for
    everything — and those need to be distinguished before anything is gated on it.

    Cheap by construction: the conversations are captured ONCE and every variant is
    a pure transform (:mod:`src.eval.multi_turn_degrade`), so the cost is one
    inference pass plus one scoring pass per variant. No extra agent calls.
    """
    from src.eval.multi_turn_degrade import DEGRADATIONS, TARGETS, degrade, describe

    base = run_multi_turn_sim(
        agent_id,
        agent_name=agent_name,
        scenario_count=scenario_count,
        max_turns=max_turns,
        score=False,
    )
    scenarios = base["_scenarios"]
    real = base["conversations"]

    import vertexai

    from src.config import GCP_PROJECT_ID, GCP_REGION
    from src.eval.batch_eval import _resolve_agent_resource_name

    vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
    resource = _resolve_agent_resource_name(agent_id)

    variants: dict[str, list[SimulatedConversation]] = {"real": real}
    for name in DEGRADATIONS:
        bad = [degrade(c, name) for c in real]
        # A degradation that changed nothing yields a variant identical to the
        # original, and a flat score would then be read as rubric blindness when it
        # is really a no-op mutation. Say so instead of scoring it.
        if all(b == c for b, c in zip(bad, real, strict=False)):
            print(f"  SKIP {name}: no-op on these conversations (nothing to degrade)")
            continue
        variants[name] = bad

    results: dict[str, Any] = {}
    for name, convos in variants.items():
        print(f"\nScoring variant: {name}")
        if name != "real":
            before, after = describe(real[0]), describe(convos[0])
            print(f"  injected: {before} -> {after}")
        results[name] = _score(scenarios, convos, resource, agent_name, score_threshold)

    baseline = results.get("real", {}).get("metrics", {})
    report: DiscriminationResult = {"baseline": baseline, "variants": {}, "targets": TARGETS}
    for name, res in results.items():
        if name == "real":
            continue
        deltas = {
            metric: round(res["metrics"].get(metric, 0.0) - value, 3)
            for metric, value in baseline.items()
        }
        target = TARGETS.get(name, "")
        hit = next((v for k, v in deltas.items() if target and target in k), None)
        report["variants"][name] = {
            "scores": res["metrics"],
            "deltas": deltas,
            "target": target,
            "target_delta": hit,
        }
    return report


def render_discrimination(report: DiscriminationResult) -> str:
    """A verdict per degradation: did the targeted rubric actually move?"""
    lines = ["", "=== DISCRIMINATION (does a known-bad conversation score lower?) ==="]
    baseline = report["baseline"]
    if not baseline:
        return "\n".join([*lines, "  no baseline scores — nothing to compare against."])

    lines.append(
        "  baseline: "
        + "  ".join(f"{k.rsplit('/', 1)[-1]}={v:.2f}" for k, v in sorted(baseline.items()))
    )
    for name, info in sorted(report["variants"].items()):
        delta = info["target_delta"]
        if delta is None:
            verdict = "NO TARGET METRIC"
        elif delta <= -0.5:
            verdict = "DISCRIMINATES"
        elif delta <= -0.2:
            verdict = "weak"
        else:
            verdict = "BLIND"
        lines.append(
            f"  {name:<18} target={info['target']:<32} delta={delta if delta is None else f'{delta:+.2f}'}  [{verdict}]"
        )
    lines += [
        "",
        "  BLIND means the rubric scored a conversation with an injected defect the",
        "  same as the real one. Until every row reads DISCRIMINATES, this metric is",
        "  a capability demo and must not gate anything.",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--agent-name", default="coordinator_agent")
    parser.add_argument("--scenario-count", type=int, default=DEFAULT_SCENARIOS)
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    parser.add_argument(
        "--score", action="store_true", help="Also score with the managed MULTI_TURN_* raters."
    )
    parser.add_argument(
        "--discriminate",
        action="store_true",
        help="Score the real conversations against deliberately broken copies, and "
        "report whether each rubric can tell them apart. Costs one scoring pass per "
        "variant; the conversations are captured once.",
    )
    parser.add_argument("--threshold", type=float, default=3.0, help="1-5 floor (scaled by /5).")
    parser.add_argument("--json", metavar="PATH", help="Write the full result JSON here.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and cost, contact nothing.",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        engine_calls = args.scenario_count * args.max_turns
        sim_calls = args.scenario_count * max(args.max_turns - 1, 0)
        print(
            f"DRY RUN — would drive {args.scenario_count} scenario(s) x up to "
            f"{args.max_turns} turns against {args.agent_id}\n"
            f"  ~{engine_calls} engine calls + ~{sim_calls} simulator calls "
            f"(upper bound; a finished plan stops earlier)\n"
            f"  scoring: {'on' if args.score else 'off'}"
        )
        return 0

    if args.discriminate:
        report = run_discrimination(
            args.agent_id,
            agent_name=args.agent_name,
            scenario_count=args.scenario_count,
            max_turns=args.max_turns,
            score_threshold=args.threshold,
        )
        print(render_discrimination(report))
        if args.json:
            import pathlib

            pathlib.Path(args.json).write_text(json.dumps(report, indent=2, default=str))
            print(f"  wrote {args.json}")
        # Non-zero when any targeted rubric failed to move: the metric is not yet
        # a signal, and a green exit would say otherwise.
        blind = [
            n
            for n, i in report["variants"].items()
            if i["target_delta"] is None or i["target_delta"] > -0.2
        ]
        if blind:
            print(f"\n  NOT A SIGNAL YET — blind to: {', '.join(sorted(blind))}")
        return 1 if blind else 0

    result = run_multi_turn_sim(
        args.agent_id,
        agent_name=args.agent_name,
        scenario_count=args.scenario_count,
        max_turns=args.max_turns,
        score=args.score,
        score_threshold=args.threshold,
    )
    print(f"\n=== Multi-turn simulation ({args.agent_name}) ===")
    print(render(result["summary"]))

    if args.json:
        import pathlib

        pathlib.Path(args.json).write_text(json.dumps(result, indent=2, default=str))
        print(f"  wrote {args.json}")

    scores = result.get("scores")
    if not scores:
        # Without --score this is a capability/shape check, so a multi-turn
        # conversation is the success criterion.
        return 0 if result["summary"]["multi_turn_conversations"] else 1

    print("\n  scores:")
    for name, value in sorted(scores["metrics"].items()):
        mark = "PASS" if value >= scores["threshold"] else "FAIL"
        print(f"    {name:<48} {value:.2f} / {scores['threshold']:.2f}  [{mark}]")
    if not scores["metrics"]:
        print("    (no metrics returned — treated as FAILURE, not a pass)")
    return 0 if scores["all_passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
