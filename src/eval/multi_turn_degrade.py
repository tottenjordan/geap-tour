"""Known-bad conversations, so the multi-turn rubrics can be shown to discriminate.

``multi_turn_sim`` scored **1.00 / 1.00 / 1.00** on its first live run. That proves
the pipeline carries data end to end. It does not prove the metric measures
anything — a rubric that returns 1.00 for everything is exactly as uninformative as
one that returns 0.00, which is the failure this repo spent 2026-09-17 removing
from four eval paths. Until a deliberately bad conversation scores lower, the
number is a capability demo, not a signal, and it must not go near the eval gate.

**Why synthesize rather than elicit.** The obvious approach is to write scenario
prompts the agent handles badly. That produces conversations of *unknown* quality:
if the agent copes, nothing was tested, and you cannot tell "the rubric missed it"
from "there was nothing to miss". Mutating a real captured conversation gives
ground truth — the defect is injected, so its absence from the score is
unambiguously the rubric's failure. Same reasoning as ``tool_faithfulness``, which
validates itself by flagging synthetic fabrications.

**Why it is nearly free.** Every degradation is a pure transform of an
already-captured conversation, so the whole experiment costs one inference pass
plus one scoring pass per variant. No extra agent calls — the same trick
``spike_metric_noise`` uses.

Each degradation targets a specific rubric, so a flat score localises the blindness:

    drop_tool_calls        -> multi_turn_tool_use_quality   (agent claims work it never did)
    stonewall              -> multi_turn_task_success       (agent never does anything)
    abandon_midway         -> multi_turn_task_success       (agent quits after turn 1)
    scramble_turns         -> multi_turn_trajectory_quality (replies answer the wrong turn)

Usage::

    from src.eval.multi_turn_degrade import DEGRADATIONS, degrade
    bad = degrade(conversation, "drop_tool_calls")
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

#: What the agent says when stonewalled — fluent, polite, and useless. Deliberately
#: not gibberish: a rubric that only catches word salad has not been tested against
#: anything a real degraded agent would produce.
STONEWALL_TEXT = (
    "Thanks for reaching out! I want to make sure I get this right for you. "
    "Could you tell me a bit more about what you're looking for?"
)


def _agent_parts(event: dict) -> list[dict]:
    content = event.get("content")
    return (content or {}).get("parts") or [] if isinstance(content, dict) else []


def _is_user(event: dict) -> bool:
    return event.get("author") == "user"


def drop_tool_calls(conversation: dict) -> dict:
    """Strip every tool call and response; keep the agent's claims intact.

    The resulting agent still says it searched flights and booked a room — it just
    never did. This is the single most important negative case for
    ``multi_turn_tool_use_quality``, which is scored from the trajectory rather
    than the text, so a rubric that still scores it well is reading the wrong
    thing entirely.
    """
    out = copy.deepcopy(conversation)
    for turn in out.get("turns", []):
        kept = []
        for event in turn.get("events", []):
            if _is_user(event):
                kept.append(event)
                continue
            parts = [
                p
                for p in _agent_parts(event)
                if not (isinstance(p, dict) and ("function_call" in p or "function_response" in p))
            ]
            # An event whose only content was the tool call disappears entirely;
            # one that also carried text keeps the text. No fallback to the
            # original parts — that would quietly resurrect what this removes.
            if parts:
                event["content"]["parts"] = parts
                kept.append(event)
        turn["events"] = kept
    # The conversation-level summary lists the tools too. Leaving it populated
    # makes the variant self-contradictory (no calls in the trace, calls in the
    # metadata) and would corrupt `summarize()` output for the degraded run.
    out["tool_calls"] = []
    return out


def stonewall(conversation: dict) -> dict:
    """Replace every agent answer with a polite request for more information.

    The user asks four times and is asked to clarify four times. Nothing is ever
    booked, checked or submitted. A conversation with zero task progress should be
    near the floor on ``multi_turn_task_success``.
    """
    out = copy.deepcopy(conversation)
    for turn in out.get("turns", []):
        kept = []
        for event in turn.get("events", []):
            if _is_user(event):
                kept.append(event)
                continue
            if isinstance(event.get("content"), dict):
                event["content"]["parts"] = [{"text": STONEWALL_TEXT}]
                kept.append(event)
        turn["events"] = kept
    out["tool_calls"] = []
    return out


def abandon_midway(conversation: dict) -> dict:
    """Keep the user's turns; delete every agent response after the first.

    Models an agent that stops responding partway through a multi-step request —
    the user keeps asking and gets nothing back. Distinct from ``stonewall``: there
    the agent replies uselessly, here it goes silent, and a rubric that scores the
    two identically is not reading turn structure.
    """
    out = copy.deepcopy(conversation)
    for i, turn in enumerate(out.get("turns", [])):
        if i == 0:
            continue
        turn["events"] = [e for e in turn.get("events", []) if _is_user(e)]
    return out


def scramble_turns(conversation: dict) -> dict:
    """Reverse the agent responses against the user turns.

    Every individual reply is well-formed and on-topic for the *conversation*; none
    answers the question actually asked. This is the pure trajectory-coherence
    case, and the hardest of the four — a rubric grading each turn in isolation
    will score it as highly as the original.

    A no-op on a conversation with fewer than two turns, where there is nothing to
    misalign; the caller should treat an unchanged conversation as untested rather
    than as a pass.
    """
    out = copy.deepcopy(conversation)
    turns = out.get("turns", [])
    if len(turns) < 2:
        return out

    agent_blocks = [[e for e in t.get("events", []) if not _is_user(e)] for t in turns]
    for turn, block in zip(turns, reversed(agent_blocks), strict=False):
        users = [e for e in turn.get("events", []) if _is_user(e)]
        turn["events"] = users + copy.deepcopy(block)
    return out


DEGRADATIONS: dict[str, Callable[[dict], dict]] = {
    "drop_tool_calls": drop_tool_calls,
    "stonewall": stonewall,
    "abandon_midway": abandon_midway,
    "scramble_turns": scramble_turns,
}

#: Which rubric each degradation is aimed at — used to report *which* blindness a
#: flat score reveals, rather than just that one exists.
TARGETS: dict[str, str] = {
    "drop_tool_calls": "multi_turn_tool_use_quality",
    "stonewall": "multi_turn_task_success",
    "abandon_midway": "multi_turn_task_success",
    "scramble_turns": "multi_turn_trajectory_quality",
}


def degrade(conversation: dict, name: str) -> dict:
    """Apply one named degradation. Raises on an unknown name rather than no-op."""
    if name not in DEGRADATIONS:
        raise KeyError(f"unknown degradation {name!r}; have {sorted(DEGRADATIONS)}")
    return DEGRADATIONS[name](conversation)


def describe(conversation: dict) -> dict[str, Any]:
    """Countable facts about a conversation, for asserting a degradation bit.

    A degradation that silently did nothing is the worst outcome here: the variant
    scores the same as the original and it reads as rubric blindness when it is
    really a broken mutation.
    """
    turns = conversation.get("turns", [])
    events = [e for t in turns for e in t.get("events", [])]
    parts = [p for e in events for p in _agent_parts(e)]
    return {
        "turns": len(turns),
        "events": len(events),
        "user_events": sum(1 for e in events if _is_user(e)),
        "agent_events": sum(1 for e in events if not _is_user(e)),
        "tool_calls": sum(1 for p in parts if isinstance(p, dict) and "function_call" in p),
        "tool_responses": sum(1 for p in parts if isinstance(p, dict) and "function_response" in p),
        "text_parts": sum(1 for p in parts if isinstance(p, dict) and p.get("text")),
    }
