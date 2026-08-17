"""Diagnostic spike: what tool calls does a deployed engine surface client-side?

**Throwaway** — this resolves ONE design question for the tool-call faithfulness
evaluator (:mod:`src.eval.tool_faithfulness`): when we drive the *deployed
coordinator* via ``stream_query``, does the event stream carry the nested
sub-agent MCP calls (``search_flights``, ``book_flight``, …), or only the
top-level ``transfer_to_agent`` delegation?

- **Branch A** — nested MCP calls are visible client-side → faithfulness runs at
  the coordinator level over the real domain tools.
- **Branch B** — only ``transfer_to_agent`` is visible → coordinator-level
  faithfulness degrades to *delegation* faithfulness; point the evaluator at the
  standalone sub-agent engines (whose MCP calls are top-level) for tool-level
  faithfulness instead.

It drives one multi-step prompt (guaranteed ≥2 domain tools) and prints, per
event, the author, the part keys, and every ``function_call`` / ``function_response``
name. **Read-only**: no metric writes, no memory writes. Record the outcome in
``docs/notes/tool-call-faithfulness.md`` and delete this module once the design is
settled.

Run:
    uv run python -m src.eval.spike_trajectory_visibility --agent-id <ENGINE_ID>
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

# A multi-step case from EVAL_CASES that forces at least two domain tool calls
# (a booking + a hotel search) — the richest probe for nested-call visibility.
PROBE_PROMPT = "Book flight FL001 for Alice Johnson, then find a hotel in New York under $350"


def _part_summary(part: dict) -> str:
    """One-line summary of a single content part (keys + any tool names)."""
    if not isinstance(part, dict):
        return f"    part (non-dict): {part!r}"
    keys = sorted(part.keys())
    bits = [f"    part keys={keys}"]
    fc = part.get("function_call")
    if isinstance(fc, dict) and fc.get("name"):
        bits.append(f"function_call={fc['name']} args={dict(fc.get('args') or {})}")
    fr = part.get("function_response")
    if isinstance(fr, dict) and fr.get("name"):
        bits.append(f"function_response={fr['name']}")
    text = part.get("text")
    if text:
        preview = str(text).strip().replace("\n", " ")[:80]
        bits.append(f'text="{preview}"')
    return "  ".join(bits)


def describe_events(events) -> None:
    """Print author + part breakdown for each stream_query event."""
    fc_names: list[str] = []
    fr_names: list[str] = []
    for i, event in enumerate(events or []):
        event = event or {}
        author = event.get("author", "?")
        parts = ((event.get("content") or {}).get("parts")) or []
        print(f"[event {i}] author={author} n_parts={len(parts)}")
        for part in parts:
            print(_part_summary(part))
            if isinstance(part, dict):
                fc = part.get("function_call")
                if isinstance(fc, dict) and fc.get("name"):
                    fc_names.append(fc["name"])
                fr = part.get("function_response")
                if isinstance(fr, dict) and fr.get("name"):
                    fr_names.append(fr["name"])

    print("\n=== SUMMARY ===")
    print(f"function_call names:     {fc_names}")
    print(f"function_response names: {fr_names}")
    non_transfer = [n for n in fc_names if n != "transfer_to_agent"]
    if non_transfer:
        print("VERDICT: Branch A — nested/domain tool calls ARE visible client-side.")
    elif fc_names:
        print("VERDICT: Branch B — only transfer_to_agent is visible (no domain tools).")
    else:
        print("VERDICT: inconclusive — no function_call parts seen (empty/cold stream?).")


def main(argv: Sequence[str] | None = None) -> int:
    """Drive one multi-step prompt through the engine and dump its event stream."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", default=None, help="engine bare id or full resource name")
    parser.add_argument("--prompt", default=PROBE_PROMPT, help="override the probe prompt")
    parser.add_argument("--user-id", default="trajectory-spike")
    args = parser.parse_args(argv)

    import vertexai
    from vertexai import agent_engines

    from src.config import AGENT_ENGINE_ID, GCP_PROJECT_ID, GCP_REGION
    from src.eval.batch_eval import _resolve_agent_resource_name

    vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
    resource = _resolve_agent_resource_name(args.agent_id or AGENT_ENGINE_ID)
    print(f"Engine: {resource}")
    print(f"Prompt: {args.prompt}\n")

    engine = agent_engines.get(resource)
    events = list(engine.stream_query(user_id=args.user_id, message=args.prompt))
    describe_events(events)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
