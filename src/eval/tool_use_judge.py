"""Standalone LLM-judge scorer for the coordinator's ``tool_use`` metric.

The coordinator batch eval wires the generic predefined
``types.RubricMetric.TOOL_USE_QUALITY`` (``agent_eval_configs.get_metrics``),
which is delegation-blind: the coordinator is a domain router whose own action on
most turns is a single ``transfer_to_agent(...)`` delegation, so a generic rubric
reads it as barely using tools and pins the score at ~0.33 for every item (a
confirmed false-negative — see ``docs/notes/coordinator-tool-use-quality.md``).

The repo already contains the fix as a *defined-but-unused* rubric:
:data:`src.eval.batch_eval.TOOL_USE_METRIC` (``name="geap_tool_use"``), whose
instruction is explicit that ``transfer_to_agent`` is the CORRECT architecture and
must not be penalized. It is a custom pointwise ``LLMMetric`` — the same type that
cannot be scored through ``client.evals`` in the installed vertexai SDK (the judge
produces a valid verdict but the service's parser rejects it as invalid JSON,
``400 Error parsing JSON``), which is exactly why ``policy_compliance`` runs
through a standalone judge (:mod:`src.eval.policy_judge`).

This module mirrors that path: it runs the deployed coordinator over the
tool-expecting cases, calls the judge model directly via ``google.genai``, and
parses the judge's ``Score: N`` line itself. The result is a legitimate 0-1
tool-use score over the *deployed* engine's responses, consumable by the
offline-eval bridge (:mod:`src.eval.publish_offline_eval`).

Honest caveat: like the policy judge, this scores the (prompt, final-response)
pair — not the raw execution trajectory. If the managed Agent Engine runtime does
not surface nested sub-agent MCP calls into the captured response, the score
reflects request/outcome quality rather than the literal tool trajectory (cause #2
in the note above, which this does not address).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from src.eval.batch_eval import EVAL_CASES, TOOL_USE_METRIC

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

DEFAULT_JUDGE_MODEL = "gemini-2.5-flash"

_SCORE_RE = re.compile(r"score\s*:?\s*\**\s*([1-5])", re.IGNORECASE)


def parse_tool_use_score(text: str | None) -> float | None:
    """Extract the judge's final ``Score: N`` (1-5) and map it to 0-1 (``N/5``).

    Returns ``None`` when no score is present so unparseable verdicts are dropped
    (not counted as zero). Uses the *last* match — judges often restate criterion
    scores before the final verdict.
    """
    if not text:
        return None
    matches = _SCORE_RE.findall(str(text))
    if not matches:
        return None
    return int(matches[-1]) / 5.0


def select_tool_use_cases(cases: Sequence[dict]) -> list[dict]:
    """Keep only cases where a tool is expected.

    Cases whose ``expected_tool`` is missing or ``"none"`` (e.g. adversarial
    prompts that must NOT call a tool) are dropped — the judge shouldn't be asked
    to reward correct tool use on a turn where no tool should have been used.
    """
    return [c for c in cases if c.get("expected_tool") not in (None, "none")]


def build_tool_use_prompt(prompt: str, response: str) -> str:
    """Fill the shared ``TOOL_USE_METRIC`` rubric with a prompt/response.

    ``TOOL_USE_METRIC.prompt_template`` is the fully-rendered delegation-aware
    rubric (instruction + criteria + rating scores + ``{prompt}``/``{response}``
    placeholders), so reusing it keeps the standalone judge on exactly the same
    rubric as the (SDK-broken) ``client.evals`` path — no rubric drift. A final
    directive nails the ``Score: N`` line the parser looks for.
    """
    template = str(TOOL_USE_METRIC.prompt_template)
    filled = template.replace("{prompt}", prompt).replace("{response}", response)
    return filled + "\n\nEnd your answer with a single line exactly: Score: <1-5>"


def score_pairs(
    pairs: Sequence[tuple[str, str]],
    generate_fn: Callable[[str], str],
) -> dict:
    """Judge each ``(prompt, response)`` pair; return the mean 0-1 score.

    ``generate_fn`` takes the rendered judge prompt and returns the judge's raw
    text. Unparseable verdicts are skipped (dropped from the average, not zeroed).
    """
    scores: list[float] = []
    for prompt, response in pairs:
        raw = generate_fn(build_tool_use_prompt(prompt, response))
        score = parse_tool_use_score(raw)
        if score is not None:
            scores.append(score)
    return {
        "score": (sum(scores) / len(scores)) if scores else None,
        "n_scored": len(scores),
        "n_total": len(pairs),
    }


def _is_error_response(response: str) -> bool:
    """True for empty/cold-start responses the inference harness couldn't parse."""
    s = str(response).strip()
    return not s or s.startswith('{"error"')


def _extract_pairs(inference_result) -> list[tuple[str, str]]:
    """Pull ``(prompt, response)`` pairs from a ``run_inference`` result frame."""
    df = getattr(inference_result, "eval_dataset_df", inference_result)
    pairs: list[tuple[str, str]] = []
    for _, row in df.iterrows():
        response = row.get("response", "")
        if _is_error_response(response):
            continue
        pairs.append((row.get("prompt", ""), response))
    return pairs


def _default_generate_fn(
    judge_model: str, project: str | None, location: str | None
) -> Callable[[str], str]:
    """Build a direct google.genai judge call (Vertex backend)."""
    from google import genai

    from src.config import GCP_PROJECT_ID, GCP_REGION

    client = genai.Client(
        vertexai=True,
        project=project or GCP_PROJECT_ID,
        location=location or GCP_REGION,
    )

    def _generate(prompt: str) -> str:
        resp = client.models.generate_content(model=judge_model, contents=prompt)
        return resp.text or ""

    return _generate


def run_tool_use_eval(
    agent_resource_name: str,
    *,
    cases: Sequence[dict] | None = None,
    client=None,
    generate_fn: Callable[[str], str] | None = None,
    judge_model: str = DEFAULT_JUDGE_MODEL,
    project: str | None = None,
    location: str | None = None,
    warm: bool = True,
) -> dict:
    """Score tool_use for the deployed coordinator over tool-expecting cases.

    Runs inference over the tool-expecting subset of the coordinator's eval cases,
    then judges each response with the delegation-aware ``geap_tool_use`` rubric
    directly (bypassing the SDK-broken ``client.evals`` custom-metric path).
    Returns ``{"score": 0-1|None, "n_scored", "n_total"}``.
    """
    from vertexai import types

    selected = select_tool_use_cases(cases if cases is not None else EVAL_CASES)

    if client is None:
        from vertexai import Client

        from src.config import GCP_PROJECT_ID, GCP_REGION

        client = Client(project=project or GCP_PROJECT_ID, location=location or GCP_REGION)

    if warm:
        try:
            from src.eval.multi_agent_batch_eval import warm_agent_engine

            engine = client.agent_engines.get(name=agent_resource_name)
            warm_agent_engine(engine)
        except Exception:  # warming is best-effort
            pass

    import pandas as pd

    session_inputs = types.evals.SessionInput(user_id="tool-use-judge-user", state={})
    df = pd.DataFrame([{"prompt": c["prompt"], "session_inputs": session_inputs} for c in selected])

    inference_result = client.evals.run_inference(agent=agent_resource_name, src=df)
    pairs = _extract_pairs(inference_result)

    gen = generate_fn or _default_generate_fn(judge_model, project, location)
    return score_pairs(pairs, gen)
