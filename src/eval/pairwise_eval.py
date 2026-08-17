"""Pairwise side-by-side (SxS) win-rate eval for the coordinator bake-off.

For an A-vs-B model comparison Google recommends a *pairwise autorater* — a judge
shown both responses that picks a winner — over diffing pointwise rubric scores:
it is more sensitive and directly answers "which is better?".

The installed vertexai SDK exposes no clean ``PairwiseMetric`` / ``AutoraterConfig``
class (only ``LLMMetric`` / ``MetricPromptBuilder`` / ``PairwiseMetricInput``),
the same SDK↔service version reality that forced :mod:`src.eval.policy_judge` off
the ``client.evals`` custom-metric path. So the shipping path here is a
standalone ``google.genai`` judge that emits ``Choice: A|B|TIE``, with the two
knobs Google's autorater config would give us implemented *for real*:

  - ``flip_enabled`` — present the pair in both orders across samples to cancel
    the judge's position bias (a judge that always picks whoever is shown first
    nets to TIE instead of a spurious win).
  - ``sampling_count`` — judge each case N times and take the majority vote,
    reducing single-sample variance.

Convention (matches the DOE main-effect direction ``claude_mean - gemini_mean``):
**baseline = Gemini** (coded ``-1``), **candidate = Claude** (coded ``+1``), so
``win_rate_candidate > 0.5`` reads as "Claude beats Gemini".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

# Winner labels.
BASELINE = "BASELINE"
CANDIDATE = "CANDIDATE"
TIE = "TIE"

DEFAULT_JUDGE_MODEL = "gemini-2.5-flash"

_CHOICE_RE = re.compile(r"choice\s*:?\s*\**\s*(A|B|TIE)\b", re.IGNORECASE)


@dataclass(frozen=True)
class PairwiseConfig:
    """Autorater knobs for the standalone pairwise judge."""

    sampling_count: int = 4
    flip_enabled: bool = True
    judge_model: str = DEFAULT_JUDGE_MODEL


def parse_ab_choice(text: str | None) -> str | None:
    """Extract the judge's final ``Choice: A|B|TIE`` (A=first shown, B=second).

    Returns ``"A"``/``"B"``/``"TIE"`` or ``None`` when no verdict is present
    (unparseable verdicts are dropped, not counted). Uses the *last* match —
    judges often restate the options before the final line.
    """
    if not text:
        return None
    matches = _CHOICE_RE.findall(str(text))
    if not matches:
        return None
    return matches[-1].upper()


def build_pairwise_prompt(prompt: str, response_a: str, response_b: str) -> str:
    """Render the pairwise judge prompt for one ordering (A first, B second)."""
    return (
        "You are an impartial judge comparing two AI assistant responses to the "
        "same user request for a corporate travel & expense assistant. Decide "
        "which response better serves the user: correct, helpful, on-policy, and "
        "appropriately refusing out-of-scope or policy-violating asks.\n\n"
        f"User request:\n{prompt}\n\n"
        f"Response A:\n{response_a}\n\n"
        f"Response B:\n{response_b}\n\n"
        "If they are equally good (or equally bad), answer TIE. "
        "End your answer with a single line exactly: Choice: <A|B|TIE>"
    )


def judge_case(
    prompt: str,
    baseline_response: str,
    candidate_response: str,
    generate_fn: Callable[[str], str],
    config: PairwiseConfig,
) -> str:
    """Judge one case ``sampling_count`` times (flip-debiased); majority winner.

    Even samples show baseline as A / candidate as B; when ``flip_enabled`` odd
    samples swap them and the parsed A/B is inverted, so pure position bias
    cancels. Returns :data:`BASELINE`, :data:`CANDIDATE`, or :data:`TIE`
    (also :data:`TIE` on a vote tie or when every sample was unparseable).
    """
    votes = {BASELINE: 0, CANDIDATE: 0, TIE: 0}
    for i in range(max(1, config.sampling_count)):
        flip = config.flip_enabled and (i % 2 == 1)
        if flip:
            raw = generate_fn(build_pairwise_prompt(prompt, candidate_response, baseline_response))
            ab = parse_ab_choice(raw)
            mapping = {"A": CANDIDATE, "B": BASELINE, "TIE": TIE}
        else:
            raw = generate_fn(build_pairwise_prompt(prompt, baseline_response, candidate_response))
            ab = parse_ab_choice(raw)
            mapping = {"A": BASELINE, "B": CANDIDATE, "TIE": TIE}
        winner = mapping.get(ab) if ab else None
        if winner:
            votes[winner] += 1

    best = max(votes, key=lambda k: votes[k])
    # A clear majority wins; a tie between baseline and candidate is a TIE.
    if best != TIE and votes[best] > max(v for k, v in votes.items() if k != best):
        return best
    if votes[BASELINE] == votes[CANDIDATE]:
        return TIE
    return BASELINE if votes[BASELINE] > votes[CANDIDATE] else CANDIDATE


def aggregate_choices(choices: Sequence[str]) -> dict:
    """Aggregate per-case winners into win/tie rates + a sign-test significance block.

    ``significance`` (from :func:`src.eval.stats.win_rate_significance`) reports the
    candidate's win-rate among *decisive* cases (ties excluded), an exact two-sided
    binomial p-value against a 50/50 null, and a Wilson CI — so a majority over a
    handful of cases is not mistaken for a real difference.
    """
    from src.eval.stats import win_rate_significance

    n = len(choices)
    cand = sum(1 for c in choices if c == CANDIDATE)
    base = sum(1 for c in choices if c == BASELINE)
    ties = sum(1 for c in choices if c == TIE)
    significance = win_rate_significance(cand, base)
    if n == 0:
        return {
            "n_cases": 0,
            "win_rate_candidate": 0.0,
            "win_rate_baseline": 0.0,
            "tie_rate": 0.0,
            "significance": significance,
        }
    return {
        "n_cases": n,
        "win_rate_candidate": cand / n,
        "win_rate_baseline": base / n,
        "tie_rate": ties / n,
        "significance": significance,
    }


def build_paired_dataset(prompts, baseline_responses, candidate_responses):
    """Assemble the SxS dataset frame (prompt + both response columns)."""
    import pandas as pd

    return pd.DataFrame(
        {
            "prompt": list(prompts),
            "baseline_response": list(baseline_responses),
            "candidate_response": list(candidate_responses),
        }
    )


# --------------------------------------------------------------------------- #
# Inference collection (mirrors src.eval.policy_judge)
# --------------------------------------------------------------------------- #
def _is_error_response(response: str) -> bool:
    """True for empty/cold-start responses the inference harness couldn't parse."""
    s = str(response).strip()
    return not s or s.startswith('{"error"')


def _responses_by_prompt(inference_result) -> dict[str, str]:
    """Map ``prompt -> response`` from a ``run_inference`` result frame."""
    df = getattr(inference_result, "eval_dataset_df", inference_result)
    out: dict[str, str] = {}
    for _, row in df.iterrows():
        out[row.get("prompt", "")] = row.get("response", "")
    return out


def _collect_responses(client, engine_id: str, cases, *, warm: bool) -> dict[str, str]:
    """Run inference for one engine over ``cases``; return ``prompt -> response``."""
    import pandas as pd

    if warm:
        try:
            from src.eval.multi_agent_batch_eval import warm_agent_engine

            warm_agent_engine(client.agent_engines.get(name=engine_id))
        except Exception:  # warming is best-effort
            pass

    df = pd.DataFrame([{"prompt": c["prompt"]} for c in cases])
    return _responses_by_prompt(client.evals.run_inference(agent=engine_id, src=df))


def _default_generate_fn(judge_model: str, project=None, location=None):
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


def run_pairwise_eval(
    baseline_engine_id: str,
    candidate_engine_id: str,
    *,
    cases: Sequence[dict] | None = None,
    config: PairwiseConfig | None = None,
    client=None,
    generate_fn: Callable[[str], str] | None = None,
    project: str | None = None,
    location: str | None = None,
    warm: bool = True,
) -> dict:
    """Run a pairwise SxS eval: baseline (Gemini) vs candidate (Claude).

    Collects each engine's response per case via ``run_inference``, pairs them,
    and has the judge pick a winner (flip-debiased, majority-voted). Cases where
    either engine returned an empty/error response are skipped. Returns win/tie
    rates plus ``per_case`` detail and the ``config`` used.
    """
    from src.eval.batch_eval import EVAL_CASES

    cases = list(cases if cases is not None else EVAL_CASES)
    config = config or PairwiseConfig()

    if client is None:
        from vertexai import Client

        from src.config import GCP_PROJECT_ID, GCP_REGION

        client = Client(project=project or GCP_PROJECT_ID, location=location or GCP_REGION)

    base_resp = _collect_responses(client, baseline_engine_id, cases, warm=warm)
    cand_resp = _collect_responses(client, candidate_engine_id, cases, warm=warm)

    gen = generate_fn or _default_generate_fn(config.judge_model, project, location)

    per_case: list[dict] = []
    choices: list[str] = []
    for c in cases:
        prompt = c["prompt"]
        b = base_resp.get(prompt, "")
        a = cand_resp.get(prompt, "")
        if _is_error_response(b) or _is_error_response(a):
            continue
        choice = judge_case(prompt, b, a, gen, config)
        choices.append(choice)
        per_case.append({"prompt": prompt, "choice": choice})

    result = aggregate_choices(choices)
    result["per_case"] = per_case
    result["config"] = {
        "sampling_count": config.sampling_count,
        "flip_enabled": config.flip_enabled,
        "judge_model": config.judge_model,
    }
    result["baseline_engine"] = baseline_engine_id
    result["candidate_engine"] = candidate_engine_id
    return result


# --------------------------------------------------------------------------- #
# Manifest helper
# --------------------------------------------------------------------------- #
def load_engines_from_manifest(manifest: dict) -> tuple[str, str]:
    """Return ``(baseline_engine, candidate_engine)`` from a bake-off manifest.

    Baseline is the ``model_backend=gemini`` point, candidate the ``claude``
    point. Each point must carry an ``engine_id`` (the persistent deployed
    engine, recorded by the bake-off orchestrator). Raises ``ValueError`` if a
    point is missing or has no engine id.
    """
    by_backend: dict[str, str] = {}
    for point in manifest.get("points", []):
        backend = point.get("assignments", {}).get("model_backend")
        engine = point.get("engine_id")
        if backend and engine:
            by_backend[backend] = engine
    if "gemini" not in by_backend or "claude" not in by_backend:
        raise ValueError(
            "manifest must have a gemini and a claude point, each with an "
            f"engine_id; got {by_backend or 'nothing usable'}"
        )
    return by_backend["gemini"], by_backend["claude"]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    """CLI: run a pairwise SxS eval from explicit engines or a bake-off manifest."""
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", help="baseline (Gemini) engine id or resource name")
    parser.add_argument("--candidate", help="candidate (Claude) engine id or resource name")
    parser.add_argument(
        "--from-manifest",
        metavar="PATH",
        help="doe_runs/<exp>/manifest.json — auto-picks gemini=baseline, claude=candidate",
    )
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--sampling-count", type=int, default=4)
    parser.add_argument("--no-flip", action="store_true", help="disable flip debiasing")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve engines + config and print the plan without running inference",
    )
    args = parser.parse_args(argv)

    if args.from_manifest:
        with open(args.from_manifest) as f:
            baseline, candidate = load_engines_from_manifest(json.load(f))
    else:
        baseline, candidate = args.baseline, args.candidate
    if not baseline or not candidate:
        parser.error("provide --baseline and --candidate, or --from-manifest")

    config = PairwiseConfig(
        sampling_count=args.sampling_count,
        flip_enabled=not args.no_flip,
        judge_model=args.judge_model,
    )

    if args.dry_run:
        print(
            "[dry-run] pairwise SxS plan:\n"
            f"  baseline (gemini):  {baseline}\n"
            f"  candidate (claude): {candidate}\n"
            f"  judge={config.judge_model} sampling={config.sampling_count} "
            f"flip={config.flip_enabled}"
        )
        return 0

    result = run_pairwise_eval(baseline, candidate, config=config)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
