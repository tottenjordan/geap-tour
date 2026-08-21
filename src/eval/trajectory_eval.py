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


def normalize_tool_name(name: str | None) -> str | None:
    """Strip the Agent Registry ``<domain>_mcp_`` prefix, if it is a real one.

    Runtime ``function_call`` events name tools as ``booking_mcp_book_flight``
    when :func:`src.registry.get_mcp_tools` resolves through Agent Registry, and
    as bare ``book_flight`` when it falls back to the direct Cloud Run URL. The
    curated references were authored against the fallback form, which was the
    normal path until the 2026-08-15 IAM remediation. Trajectory scoring compares
    names *literally*, so both sides must be in one form.

    Only strips when the domain is a real server (``verify_mcp_tools.EXPECTED_TOOLS``)
    **and** the remainder is one of that domain's tools — a normalizer that guesses
    would silently rename an unrelated tool. Anything else is returned unchanged.
    """
    if not name:
        return name
    from src.eval.verify_mcp_tools import EXPECTED_TOOLS

    domain, sep, tool = name.partition("_mcp_")
    if sep and tool in EXPECTED_TOOLS.get(domain, ()):
        return tool
    return name


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


def returned_tool_names(events) -> set[str]:
    """Bare tool names present in any ``function_response`` part.

    Mirrors :func:`extract_trajectory` but reads the *response* side: each
    ``function_response`` part carries ``{"name", "response"}``. Returns the set
    of names for which a result was observed — the "did it actually return?" side
    used to annotate a captured trajectory. ``[]``/``None`` input yields an empty
    set.
    """
    names: set[str] = set()
    for event in events or []:
        parts = ((event or {}).get("content") or {}).get("parts") or []
        for part in parts:
            if not isinstance(part, dict):
                continue
            fr = part.get("function_response")
            if not fr:
                continue
            name = fr.get("name")
            if name:
                names.add(name)
    return names


def capture_trajectory(events, *, include_transfers: bool = False) -> list[dict]:
    """:func:`extract_trajectory` + a per-call ``returned: bool`` flag.

    Reuses :func:`extract_trajectory` for the ordered call side
    (``{"tool_name", "tool_input"}``) and annotates each call with whether a
    matching ``function_response`` was observed in the same stream. This is the
    ground-truth surface the faithfulness judge compares response claims against
    (see :mod:`src.eval.tool_faithfulness`). Existing ``extract_trajectory``
    callers are untouched — only this function adds the ``returned`` key.
    """
    returned = returned_tool_names(events)
    calls = extract_trajectory(events, include_transfers=include_transfers)
    for call in calls:
        call["returned"] = call["tool_name"] in returned
    return calls


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

    Vertex ``EvalTask`` invokes ``runnable.query(input=<prompt>)`` (it matches the
    runtime-checkable ``reasoning_engines.Queryable`` protocol) and reads
    ``response`` and ``predicted_trajectory`` from the returned dict.

    Two robustness measures, both learned the hard way:

    * **Raw-SSE fallback.** A recycled engine streams NDJSON that the installed
      ``google-api-core`` array-only parser rejects; every other stream consumer in
      this repo already falls back to :mod:`src.eval.raw_stream` and this one did
      not (memory ``agent-engine-sse-parse-skew``).
    * **Empty-turn retry.** ``EvalTask`` fans the dataset out concurrently, which is
      exactly the pattern that makes a warm-but-busy engine return empty-at-200
      turns (``_sdk_patches`` throttles the batch path for the same reason). An
      empty trajectory is not merely a zero here — the evaluation API rejects it
      with "Required field is not set", which turns *every* metric into ``nan``.

    Tool names are normalized (:func:`normalize_tool_name`) because the reference
    trajectories are bare and the runtime emits registry-prefixed names.
    """

    def __init__(
        self,
        engine,
        *,
        user_id: str = "trajectory-eval",
        include_transfers: bool = False,
        empty_retries: int = 2,
    ):
        self._engine = engine
        self._user_id = user_id
        self._include_transfers = include_transfers
        self._empty_retries = max(1, empty_retries)

    def _stream(self, prompt: str) -> list[dict]:
        """One ``stream_query`` pass with the SSE-parse-skew fallback."""
        from src.eval import raw_stream

        try:
            return list(self._engine.stream_query(user_id=self._user_id, message=prompt))
        except ValueError as exc:
            resource = raw_stream.agent_resource_name(self._engine)
            if not raw_stream.is_sse_parse_skew(exc) or not resource:
                raise
            sid = raw_stream.create_session(resource, self._user_id)
            return raw_stream.stream_query_events(
                resource, message=prompt, user_id=self._user_id, session_id=sid
            )

    def query(self, input: str = "", **kwargs) -> dict:  # SDK invokes query(input=<prompt>)
        trajectory: list[dict] = []
        events: list[dict] = []
        for _ in range(self._empty_retries):
            events = self._stream(input)
            trajectory = extract_trajectory(events, include_transfers=self._include_transfers)
            if trajectory:
                break
        for call in trajectory:
            call["tool_name"] = normalize_tool_name(call["tool_name"])
        return {"response": _final_text(events), "predicted_trajectory": trajectory}


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

    Filters ``cases`` to those carrying a ``reference_trajectory``, generates each
    prediction **here** (serially, via :class:`CoordinatorRunnable`), drops the turns
    that produced no tool call at all, and scores the rest with ``EvalTask`` in
    bring-your-own-response mode. Returns
    ``{"scored_cases", "empty_trajectories", "metrics"}``. A clean no-op (no
    ``EvalTask`` constructed) when nothing is left to score.

    **Why not hand the runnable to ``EvalTask``** (which it supports): it fans the
    dataset out concurrently, which is the documented trigger for empty-at-200 turns
    on a busy engine, and the evaluation API rejects an empty ``predicted_trajectory``
    with "Required field is not set" rather than scoring it 0 — one empty row is
    enough to turn *every* metric into ``nan``. Measured 2026-08-21: handing over the
    runnable produced ``failure/mean 1.0`` and all-``nan`` metrics. Generating serially
    and partitioning the empties out mirrors what the batch eval does with
    ``infra_empty_rate`` (docs/notes/offline-eval-empty-turns.md), and keeps an infra
    failure from being reported as a trajectory score.

    ``eval_task_cls`` and ``runnable`` are injectable for offline wiring tests.
    """
    if cases is None:
        from src.eval.agent_eval_configs import get_eval_cases

        cases = get_eval_cases("coordinator_agent")

    with_reference = [c for c in cases if c.get("reference_trajectory")]
    if not with_reference:
        return {"scored_cases": 0, "empty_trajectories": 0, "metrics": {}}

    runner = runnable or CoordinatorRunnable(engine)
    rows = []
    empty = 0
    for case in with_reference:
        predicted = (runner.query(input=case["prompt"]) or {}).get("predicted_trajectory") or []
        if not predicted:
            empty += 1
            continue
        rows.append(
            {
                "prompt": case["prompt"],
                "reference_trajectory": _reference_trajectory(case),
                # Args are blanked on BOTH sides. The metrics compare the whole
                # {tool_name, tool_input} dict, and `reference_trajectory` is a list
                # of tool *names* by design — so leaving real args on the predicted
                # side scores every row 0 (measured 2026-08-21: exact_match /
                # precision / recall all 0.00 with args, on turns whose tool
                # sequence was in fact correct). This is deliberately a name-and-
                # order metric; argument fidelity is covered by tool_faithfulness
                # and the geap_tool_use rubric.
                "predicted_trajectory": [
                    {"tool_name": call["tool_name"], "tool_input": {}} for call in predicted
                ],
            }
        )

    if not rows:
        return {"scored_cases": 0, "empty_trajectories": empty, "metrics": {}}

    import pandas as pd

    dataset = pd.DataFrame(rows)

    if eval_task_cls is None:
        from vertexai.preview.evaluation import EvalTask

        eval_task_cls = EvalTask

    task = eval_task_cls(dataset=dataset, metrics=list(TRAJECTORY_METRICS), experiment=experiment)
    # No `runnable=` — the predictions are already in the dataset (BYOR mode).
    result = task.evaluate()
    return {
        "scored_cases": len(rows),
        "empty_trajectories": empty,
        "metrics": dict(getattr(result, "summary_metrics", {}) or {}),
    }
