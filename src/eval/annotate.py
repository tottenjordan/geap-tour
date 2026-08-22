"""Collect a BLIND second annotation pass over the calibration gold set.

Judge-vs-human agreement is uninterpretable without a human-vs-human ceiling (see
:mod:`src.eval.calibration`). This CLI collects that second pass.

Four design constraints, each one load-bearing:

**Blind.** Existing ``annotations``, the derived ``human_score``, and every judge
score are hidden. Anchoring is the entire risk of a second pass by the same
operator — a number glimpsed once cannot be un-seen, and the resulting "agreement"
would measure recall, not judgement.

**Grounded.** Each case is shown above the *real* policy table, rendered from
``mock_db.POLICY_LIMITS`` rather than restated here so it cannot drift. This is
the direct fix for how v1 of the gold set went wrong: its reference answers
asserted a $200 entertainment limit, city-specific lodging caps and a "gifts"
category, none of which exist. Annotating from memory is what produced that.

**Resumable.** Scores append to ``data/annotations_<id>.json`` after every case and
already-scored prompts are skipped. ~50 cases is more than one sitting, and a
tired annotator is a noisy one.

**Non-destructive.** A session never touches the committed gold set; ``--merge``
folds a completed pass in as a separate, reviewable step.

Usage:
  uv run python -m src.eval.annotate --annotator a2            # annotate (resumable)
  uv run python -m src.eval.annotate --annotator a2 --status   # progress, no prompts
  uv run python -m src.eval.annotate --merge a2                # fold into the gold set

The render/parse/merge helpers are pure and unit-tested; only :func:`run_session`
touches stdin.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path
from typing import TYPE_CHECKING

from src.eval.calibration import GOLD_SET_PATH

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

DATA_DIR = Path(__file__).parent / "data"

# The rubric the annotator scores against. Deliberately short: a long rubric read
# 50 times drifts, and the policy table below carries the facts.
RUBRIC = """\
Score how well the reply handles CORPORATE EXPENSE POLICY, 1-5:
  5  correct verdict from a real limit, clearly explained
     — or policy simply did not apply and the reply rightly said nothing about it
  4  correct limit and verdict, explanation thin
  3  right limit, but the verdict or explanation is incomplete
  2  a real limit applied to reach the wrong verdict
  1  states a limit or rule that does not exist, gets a real limit wrong,
     refuses to submit an over-limit expense, or ignores policy where it applied"""


def policy_table() -> str:
    """The real limits, rendered from the system so this can never drift."""
    from src.mcp_servers.expense.mock_db import POLICY_LIMITS

    rows = "\n".join(f"    {cat:<14} ${limit:,.0f}" for cat, limit in sorted(POLICY_LIMITS.items()))
    return (
        "THE POLICY IS EXACTLY THIS AND NOTHING MORE:\n"
        f"{rows}\n"
        "    No city caps, no per-person scaling, no travel-class rules, no\n"
        "    pre-approval tiers, no other categories.\n"
        "    An over-limit expense is STILL SUBMITTED and flagged for manager\n"
        "    review — refusing to submit it is a defect, not caution."
    )


def annotations_path(annotator: str) -> Path:
    return DATA_DIR / f"annotations_{annotator}.json"


def case_key(case: dict) -> str:
    """Identify a case by prompt+response — the gold set has repeated prompts."""
    return f"{case['prompt']}||{case['response']}"


def load_progress(annotator: str) -> dict:
    path = annotations_path(annotator)
    return json.loads(path.read_text()) if path.exists() else {}


def save_progress(annotator: str, progress: dict) -> None:
    annotations_path(annotator).write_text(json.dumps(progress, indent=2, sort_keys=True) + "\n")


def pending(cases: Sequence[dict], progress: dict, annotator: str) -> list[dict]:
    """Cases this annotator has not scored yet, in a stable shuffled order.

    Seeded from the annotator id so a resumed session keeps its ordering, and so
    two annotators see the cases in *different* orders — order effects are real,
    and having both annotators fatigue on the same cases would correlate their
    errors and inflate agreement.
    """
    todo = [c for c in cases if case_key(c) not in progress]
    # Seeded PRNG: this decides display ORDER only, nothing security-relevant.
    rng = random.Random(annotator)
    rng.shuffle(todo)
    return todo


def render_case(case: dict, index: int, total: int) -> str:
    """The blind prompt shown for one case. Carries NO existing score."""
    return (
        f"\n{'=' * 72}\n"
        f"Case {index}/{total}   [{case.get('difficulty', '?')}]\n"
        f"{'=' * 72}\n"
        f"USER ASKED:\n  {case['prompt']}\n\n"
        f"ASSISTANT REPLIED:\n  {case['response']}\n"
    )


def parse_input(raw: str) -> tuple[int | None, str]:
    """Parse ``"4"`` or ``"4 limit right, verdict vague"`` -> ``(4, note)``.

    Returns ``(None, "")`` for anything that is not a 1-5 score, so the caller can
    re-prompt rather than silently recording a wrong number.
    """
    text = (raw or "").strip()
    if not text:
        return None, ""
    head, _, tail = text.partition(" ")
    if head.isdigit() and 1 <= int(head) <= 5:
        return int(head), tail.strip()
    return None, ""


def merge_annotations(gold: dict, annotator: str, progress: dict) -> tuple[dict, int]:
    """Fold a completed pass into the gold structure; returns (gold, n_applied).

    ``human_score`` is recomputed as the median of all annotators so the derived
    field never drifts from its source.
    """
    from src.eval.calibration import consensus_score

    applied = 0
    for case in gold["cases"]:
        entry = progress.get(case_key(case))
        if entry is None:
            continue
        case.setdefault("annotations", {})[annotator] = entry["score"]
        if entry.get("note"):
            case.setdefault("notes", {})[annotator] = entry["note"]
        applied += 1
    for case in gold["cases"]:
        consensus = consensus_score(case)
        if consensus is not None:
            case["human_score"] = int(consensus) if consensus == int(consensus) else consensus
    return gold, applied


def run_session(
    annotator: str,
    cases: Sequence[dict],
    progress: dict,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    save_fn: Callable[[str, dict], None] = save_progress,
) -> dict:
    """Interactive loop. Every dependency injected so tests never touch stdin."""
    todo = pending(cases, progress, annotator)
    if not todo:
        output_fn(f"Nothing left to annotate for '{annotator}' ({len(progress)} done).")
        return progress

    output_fn(policy_table())
    output_fn(f"\n{RUBRIC}\n")
    output_fn(f"{len(todo)} case(s) to score. Enter a score, optionally + a note.")
    output_fn("Enter 'q' to stop — progress is saved after every case.\n")

    for i, case in enumerate(todo, 1):
        output_fn(render_case(case, i, len(todo)))
        while True:
            raw = input_fn("score 1-5 (+ optional note) > ")
            if raw.strip().lower() == "q":
                output_fn(f"Stopped. {len(progress)} scored so far — rerun to resume.")
                return progress
            score, note = parse_input(raw)
            if score is not None:
                break
            output_fn("  Please enter a number 1-5 (e.g. '4' or '4 verdict is vague').")
        progress[case_key(case)] = {"score": score, "note": note}
        save_fn(annotator, progress)
    output_fn(f"\nDone — {len(progress)} cases scored. Now run: --merge {annotator}")
    return progress


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Blind annotation pass over the gold set.")
    parser.add_argument("--annotator", help="annotator id, e.g. a2")
    parser.add_argument("--merge", metavar="ID", help="fold a completed pass into the gold set")
    parser.add_argument("--status", action="store_true", help="show progress and exit")
    args = parser.parse_args(argv)

    gold = json.loads(GOLD_SET_PATH.read_text())
    cases = gold["cases"]

    if args.merge:
        progress = load_progress(args.merge)
        if not progress:
            print(f"No annotations found for '{args.merge}' — nothing to merge.")
            return 1
        gold, applied = merge_annotations(gold, args.merge, progress)
        GOLD_SET_PATH.write_text(json.dumps(gold, indent=2) + "\n")
        print(f"Merged {applied} annotation(s) from '{args.merge}' into the gold set.")
        print("Re-run `python -m src.eval.calibration --annotators` to see the human ceiling.")
        return 0

    if not args.annotator:
        parser.error("one of --annotator or --merge is required")

    progress = load_progress(args.annotator)
    if args.status:
        print(f"{args.annotator}: {len(progress)}/{len(cases)} scored")
        return 0

    run_session(args.annotator, cases, progress)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
