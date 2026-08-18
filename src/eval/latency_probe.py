"""Per-phase latency attribution for a deployed coordinator engine.

The managed ``reasoning_engine/request_latencies`` metric reports one number per
request (~17s p50 / 51s p95 live). This probe breaks that single number down so
we can see *where* the time goes, by streaming a turn and stamping every SSE
event with a wall-clock time, then bucketing the inter-event gaps:

* ``startup``  — request start → first event: client guardrail + Memory Bank
  preload + server-side Model Armor screen + the first LLM call (and any
  cold-start).
* ``mcp_tool`` — a ``function_call`` event → its ``function_response`` event: the
  MCP tool round-trip to Cloud Run (search/booking/expense).
* ``llm``      — a ``function_response`` (or the prior turn) → the next model
  event: the LLM's next generation.

Read-only: it POSTs ``:streamQuery`` against the live engine (billable) and
writes nothing. Runs a few graded prompts (trivial / single-tool / multi-tool)
so the tool-hop contribution is visible as the prompt gets more complex.

    uv run python -m src.eval.latency_probe --agent-id 4380288848559603712
"""

from __future__ import annotations

import argparse
import time

from src.eval import raw_stream

# Graded prompts: 0 tools, 1 tool, then a multi-tool booking+search chain. The
# jump in total time across these isolates the per-tool-hop cost from startup.
DEFAULT_PROMPTS = [
    "Hello!",
    "Search for flights from JFK to LAX",
    "Book flight FL001 for Alice Johnson, then find a hotel in New York under $350",
]


def _classify(event: dict) -> str:
    """Coarse event kind for gap attribution: response / call / text / other."""
    parts = ((event or {}).get("content") or {}).get("parts") or []
    has_response = any(isinstance(p, dict) and p.get("function_response") for p in parts)
    has_call = any(isinstance(p, dict) and p.get("function_call") for p in parts)
    has_text = any(isinstance(p, dict) and p.get("text") for p in parts)
    if has_response:
        return "response"
    if has_call:
        return "call"
    if has_text:
        return "text"
    return "other"


def _call_names(event: dict) -> list[str]:
    parts = ((event or {}).get("content") or {}).get("parts") or []
    return [
        p["function_call"]["name"]
        for p in parts
        if isinstance(p, dict) and p.get("function_call") and p["function_call"].get("name")
    ]


def stream_timed(resource_name: str, prompt: str, *, user_id: str, token: str) -> dict:
    """Stream one turn, stamping each event; return a phase-attributed timeline.

    Attribution buckets the gap *before* each event by that event's kind:
    a ``response`` gap is the tool round-trip that just completed (``mcp_tool``);
    a ``call``/``text`` gap is an LLM generation (``llm``). The gap before the
    very first event is ``startup``.
    """
    import requests

    sid = raw_stream.create_session(resource_name, user_id, token=token)
    base = raw_stream._endpoint_base(resource_name)
    t_start = time.monotonic()
    resp = requests.post(
        f"{base}/{resource_name}:streamQuery?alt=sse",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "class_method": "stream_query",
            "input": {"user_id": user_id, "session_id": sid, "message": prompt},
        },
        stream=True,
        timeout=180,
    )

    buckets = {"startup": 0.0, "mcp_tool": 0.0, "llm": 0.0}
    tool_calls: list[str] = []
    n_events = 0
    prev = t_start
    first = True
    for raw in resp.iter_lines(decode_unicode=True):
        event = raw_stream.parse_sse_line(raw)
        if event is None:
            continue
        now = time.monotonic()
        gap = now - prev
        kind = _classify(event)
        if first:
            buckets["startup"] += gap
            first = False
        elif kind == "response":
            buckets["mcp_tool"] += gap
        else:
            buckets["llm"] += gap
        tool_calls.extend(_call_names(event))
        n_events += 1
        prev = now

    total = time.monotonic() - t_start
    domain_tools = [t for t in tool_calls if t != "transfer_to_agent"]
    return {
        "prompt": prompt,
        "total_s": total,
        "buckets": buckets,
        "n_events": n_events,
        "tool_calls": tool_calls,
        "n_domain_tools": len(domain_tools),
    }


def _fmt(result: dict) -> str:
    b = result["buckets"]
    return (
        f"  total {result['total_s']:6.1f}s | "
        f"startup {b['startup']:5.1f}s  mcp {b['mcp_tool']:5.1f}s  llm {b['llm']:5.1f}s | "
        f"{result['n_domain_tools']} tools {result['tool_calls']}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", required=True, help="Engine ID or full resource name")
    parser.add_argument("--rounds", type=int, default=2, help="Passes over the prompts (default 2)")
    parser.add_argument("--user-id", default="latency-probe")
    args = parser.parse_args(argv)

    from src.eval.batch_eval import _resolve_agent_resource_name

    resource = _resolve_agent_resource_name(args.agent_id)
    token = raw_stream._default_token()
    print(f"Latency probe → {resource}\n")

    for r in range(args.rounds):
        label = "cold" if r == 0 else f"warm#{r}"
        print(f"=== round {r} ({label}) ===")
        for prompt in DEFAULT_PROMPTS:
            try:
                result = stream_timed(resource, prompt, user_id=args.user_id, token=token)
                print(f"[{prompt[:48]!r}]")
                print(_fmt(result))
            except Exception as exc:  # diagnostic tool — report, don't crash the run
                print(f"[{prompt[:48]!r}]  x {type(exc).__name__}: {exc}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
