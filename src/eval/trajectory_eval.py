"""Deterministic trajectory / tool-use evaluation via Vertex ``EvalTask``.

The rubric metrics in :mod:`src.eval.batch_eval` are LLM-judged and adaptive.
This module adds the *deterministic* half of the comparison: it scores the
coordinator's ordered tool-call sequence against curated ``reference_trajectory``
lists using programmatic set/sequence metrics (``trajectory_exact_match``,
``trajectory_precision``, ``trajectory_recall``). No autorater, no sampling —
the same inputs always produce the same score, which makes this a cheap,
reproducible regression signal to sit alongside the rubric means and the
pairwise SxS win-rate in the bake-off.

Only coordinator eval cases that carry a ``reference_trajectory`` are scored;
cases without one are skipped (trajectory metrics need a ground-truth path).

Tool-name convention: references use the *bare* ADK tool names
(``search_flights``, ``book_flight``, …) — the same names that appear in
``stream_query`` ``function_call`` events and in the curated evalset files — not
the ``search_mcp_*`` prefixed form used by the ``expected_tool`` field. The ADK
``transfer_to_agent`` delegation call is plumbing, not a domain tool, so it is
filtered out of the predicted trajectory by default.
"""

from __future__ import annotations

# Deterministic (non-LLM) trajectory metrics, by their EvalTask literal names.
# These are the string values of vertexai.preview.evaluation.constants.Metric
# .TRAJECTORY_{EXACT_MATCH,PRECISION,RECALL}; spelled out here so the EvalTask
# constructor's Literal-typed `metrics` param type-checks (a list of the const
# objects widens to list[str]).
TRAJECTORY_METRICS = ("trajectory_exact_match", "trajectory_precision", "trajectory_recall")


def extract_trajectory(events, *, include_transfers: bool = False) -> list[dict]:
    """Ordered ``[{"tool_name", "tool_input"}]`` from ``stream_query`` events.

    Scans each event's ``content.parts`` for ``function_call`` parts and records
    them in call order. ``transfer_to_agent`` delegation calls are dropped unless
    ``include_transfers`` is set. Returns ``[]`` for ``None``/empty input.
    """
    trajectory: list[dict] = []
    for event in events or []:
        parts = ((event or {}).get("content") or {}).get("parts") or []
        for part in parts:
            if not isinstance(part, dict):
                continue
            fc = part.get("function_call")
            if not fc:
                continue
            name = fc.get("name")
            if not name:
                continue
            if not include_transfers and name == "transfer_to_agent":
                continue
            trajectory.append({"tool_name": name, "tool_input": dict(fc.get("args") or {})})
    return trajectory


def _final_text(events) -> str:
    """Last text part across the event stream, or ``""`` (tool-only turn)."""
    found = ""
    for event in events or []:
        parts = ((event or {}).get("content") or {}).get("parts") or []
        for part in parts:
            if isinstance(part, dict) and part.get("text"):
                found = part["text"]
    return found


class CoordinatorRunnable:
    """``EvalTask`` runnable wrapping a deployed coordinator engine.

    Vertex ``EvalTask`` invokes ``runnable.query(input=<prompt>)`` and reads
    ``response`` and ``predicted_trajectory`` from the returned dict.
    """

    def __init__(
        self, engine, *, user_id: str = "trajectory-eval", include_transfers: bool = False
    ):
        self._engine = engine
        self._user_id = user_id
        self._include_transfers = include_transfers

    def query(self, input: str = "", **kwargs) -> dict:  # SDK invokes query(input=<prompt>)
        events = list(self._engine.stream_query(user_id=self._user_id, message=input))
        return {
            "response": _final_text(events),
            "predicted_trajectory": extract_trajectory(
                events, include_transfers=self._include_transfers
            ),
        }


def _reference_trajectory(case: dict) -> list[dict]:
    """Materialize a case's bare tool-name list into EvalTask trajectory dicts."""
    return [{"tool_name": name, "tool_input": {}} for name in case["reference_trajectory"]]


def run_trajectory_eval(
    engine,
    cases: list[dict] | None = None,
    *,
    eval_task_cls=None,
    runnable=None,
    experiment: str | None = None,
) -> dict:
    """Score the coordinator's tool-call trajectories deterministically.

    Filters ``cases`` to those carrying a ``reference_trajectory``, builds an
    ``EvalTask`` dataset (``prompt`` + materialized ``reference_trajectory``),
    runs it against a :class:`CoordinatorRunnable` over ``engine``, and returns
    ``{"scored_cases": int, "metrics": {<name>: <mean>}}``. A clean no-op
    (no EvalTask constructed) when no case has a reference trajectory.

    ``eval_task_cls`` and ``runnable`` are injectable for offline wiring tests.
    """
    if cases is None:
        from src.eval.agent_eval_configs import get_eval_cases

        cases = get_eval_cases("coordinator_agent")

    scored = [c for c in cases if c.get("reference_trajectory")]
    if not scored:
        return {"scored_cases": 0, "metrics": {}}

    import pandas as pd

    dataset = pd.DataFrame(
        [{"prompt": c["prompt"], "reference_trajectory": _reference_trajectory(c)} for c in scored]
    )

    if eval_task_cls is None:
        from vertexai.preview.evaluation import EvalTask

        eval_task_cls = EvalTask

    task = eval_task_cls(dataset=dataset, metrics=list(TRAJECTORY_METRICS), experiment=experiment)
    result = task.evaluate(runnable=runnable or CoordinatorRunnable(engine))
    return {
        "scored_cases": len(scored),
        "metrics": dict(getattr(result, "summary_metrics", {}) or {}),
    }
