"""Throwaway diagnostic: categorize the router's end-to-end streaming per tier.

For each prompt we open a fresh session and stream one turn via the raw-SSE
client (bypasses the SDK NDJSON-parse skew), then classify the outcome:

    events            total parsed event dicts
    transfer          a ``transfer_to_agent`` function_call appeared (router root
                      made the routing decision)
    active_authors    distinct non-root event authors (which tier agent ran)
    mcp_calls         domain MCP function_call names (search/booking/expense) —
                      the specialist actually used a tool
    final_chars       length of the visible final text

Outcome buckets:
    EMPTY             0-1 events, no final text
    TRANSFER_ONLY     transfer emitted but no MCP call / no specialist text
    FULL              transfer + specialist MCP call + non-empty final text

Read-only; no writes, no redeploy.

Run:
    uv run python -m src.eval.spike_router_streaming --agent-id 6134089059699523584
"""

from __future__ import annotations

import argparse
import time
from typing import TYPE_CHECKING

from src.eval.batch_eval import _resolve_agent_resource_name
from src.eval.raw_stream import create_session, stream_query_events
from src.eval.trajectory_eval import extract_trajectory
from src.traffic.generate_traffic import _extract_text

if TYPE_CHECKING:
    from collections.abc import Sequence

# Tier-spanning probes (simple → complex) to exercise different routes.
PROBES = [
    ("lite", "What is the meal expense limit?"),
    ("flash", "Search for flights from SFO to JFK next Monday."),
    ("pro", "Compare flights SFO->JFK vs SFO->BOS and tell me which is cheaper."),
    (
        "complex",
        "Book flight FL001 for Alice Johnson, then find a hotel in New York under $350.",
    ),
]

DOMAIN_PREFIXES = ("search_", "book_", "check_", "submit_", "get_user_")


def _mcp_call_names(events) -> list[str]:
    """Domain MCP function_call names across all events (any author)."""
    names: list[str] = []
    for ev in events:
        content = ev.get("content") or {}
        for part in content.get("parts") or []:
            fc = part.get("function_call")
            if fc and any(str(fc.get("name", "")).find(p) >= 0 for p in DOMAIN_PREFIXES):
                names.append(fc["name"])
    return names


def _authors(events) -> list[str]:
    return sorted({ev.get("author") for ev in events if ev.get("author")})


def _saw_transfer(events) -> bool:
    for ev in events:
        content = ev.get("content") or {}
        for part in content.get("parts") or []:
            fc = part.get("function_call")
            if fc and fc.get("name") == "transfer_to_agent":
                return True
    return False


def classify(events) -> str:
    """New (direct-tools) router: success == a non-empty final response streamed.

    (The old transfer_to_agent buckets are gone — the router no longer delegates.)
    """
    final = "".join(_extract_text(e) for e in events).strip()
    if final:
        return "FULL"
    if len(events) <= 1:
        return "EMPTY"
    return "PARTIAL"


def _session_with_retry(resource, user_id, *, attempts=5):
    """create_session with backoff — the shared session service 400s/KeyErrors
    under rapid probing (documented ceiling), so retry with widening spacing."""
    last = None
    for a in range(attempts):
        try:
            return create_session(resource, user_id)
        except Exception as exc:
            last = exc
            time.sleep(3 * (a + 1))
    raise RuntimeError(f"create_session failed after {attempts} attempts: {last}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Router streaming diagnostic")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--repeat", type=int, default=1, help="Runs per probe")
    parser.add_argument("--user-id", default="router-streaming-spike")
    args = parser.parse_args(argv)

    resource = _resolve_agent_resource_name(args.agent_id)
    print(f"Router engine: {resource}\n")

    tally: dict[str, int] = {}
    for label, prompt in PROBES:
        for i in range(args.repeat):
            # Retry an EMPTY stream once — cold-start empties are a known runtime
            # artifact distinct from a genuine routing/streaming failure.
            for attempt in range(2):
                sid = _session_with_retry(resource, args.user_id)
                events = stream_query_events(
                    resource, message=prompt, user_id=args.user_id, session_id=sid
                )
                bucket = classify(events)
                if bucket != "EMPTY" or attempt == 1:
                    break
                time.sleep(4)
            tally[bucket] = tally.get(bucket, 0) + 1
            traj = [c["tool_name"] for c in extract_trajectory(events)]
            final = "".join(_extract_text(e) for e in events).strip()
            print(
                f"[{label:8}#{i}] {bucket:13} "
                f"events={len(events):2} transfer={_saw_transfer(events)!s:5} "
                f"authors={_authors(events)} mcp={traj} final_chars={len(final)}"
            )
            time.sleep(4)  # space out session creation (shared-service ceiling)

    print("\n=== tally ===")
    for bucket, n in sorted(tally.items()):
        print(f"  {bucket:13} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
