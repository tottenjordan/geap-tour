"""Spike: could the 5-tier router be a ``google.adk.workflow`` graph? (2026-09-08)

The router is the largest hand-rolled subsystem here — a ``before_agent_callback``
classifier plus :class:`src.router.tier_routing_llm.TierRoutingLlm`, which swaps the
model per turn on ONE direct-tools agent. ADK 2.8.0 ships ``google.adk.workflow``,
the first-party graph runtime, whose conditional routes are the obvious equivalent.

**This is a measurement, not a migration.** The current design exists *because*
``transfer_to_agent`` + ``sub_agents`` never streamed the specialist's turn through
the managed runtime (docs/notes/router-transfer-streaming.md). A graph rewrite that
cannot stream is the same bug in new syntax, so streaming is question one.

Run it::

    uv run python -m src.eval.spike_graph_router

Findings are summarised in docs/notes/router-transfer-streaming.md; re-run this when
ADK's graph runtime moves, because the verdict is about maturity, not impossibility.
"""

from __future__ import annotations

import argparse
import sys

DEFAULT_PROMPT = "Summarize the tradeoffs of quantum computing in two sentences."


def build_tier_graph(route_to: str = "flash"):
    """A two-tier routing graph: classifier -> {lite, flash}.

    Deliberately minimal. The question is whether the *mechanism* streams, not
    whether a full 5-tier port is correct.

    Note the route mechanism: a node must **yield an Event carrying
    ``EventActions(route=...)``**. Returning the route key as a plain value does
    NOT route — see :func:`probe`.
    """
    from google.adk.agents import LlmAgent
    from google.adk.events import Event, EventActions
    from google.adk.workflow import START, Workflow, node

    from src.config import resolve_model

    @node(name="classify")
    def classify():
        yield Event(author="classify", actions=EventActions(route=route_to))

    lite = LlmAgent(
        name="lite_node",
        model=resolve_model("gemini-2.5-flash-lite"),
        instruction="Answer concisely.",
    )
    flash = LlmAgent(
        name="flash_node",
        model=resolve_model("gemini-2.5-flash"),
        instruction="Answer concisely.",
    )
    return Workflow(
        name="tier_router",
        edges=[(START, classify), (classify, {"lite": lite, "flash": flash})],
    )


def stream_chars(app, message: str) -> tuple[int, int]:
    """Drive one turn; return ``(events, text_chars)``."""
    events = chars = 0
    for ev in app.stream_query(user_id="spike-graph-router", message=message):
        events += 1
        for part in ((ev or {}).get("content") or {}).get("parts", []) or []:
            chars += len(part.get("text") or "")
    return events, chars


def probe(message: str = DEFAULT_PROMPT) -> dict:
    """Answer the three questions the spike exists to answer."""
    import vertexai
    from vertexai import agent_engines

    from src.config import GCP_PROJECT_ID, GCP_REGION

    vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
    results: dict = {}

    # Q0 — is a Workflow a BaseAgent? Everything in this repo assumes root_agent is.
    from google.adk.agents import BaseAgent
    from google.adk.workflow import Workflow

    results["workflow_is_base_agent"] = issubclass(Workflow, BaseAgent)

    # Q1 — does conditional routing stream at all?
    app = agent_engines.AdkApp(agent=build_tier_graph("flash"))
    app.set_up()
    events, chars = stream_chars(app, message)
    results["routed_events"] = events
    results["routed_chars"] = chars
    results["streams"] = chars > 0

    # Q2 — what does a MIS-emitted route do? (The failure mode that matters.)
    # A plain `return "flash"` sets the node's output, not its route.
    from google.adk.agents import LlmAgent
    from google.adk.workflow import START, node

    from src.config import resolve_model

    @node(name="classify_returns_instead_of_yielding")
    def bad_classify() -> str:
        return "flash"

    bad = Workflow(
        name="tier_router_bad",
        edges=[
            (START, bad_classify),
            (
                bad_classify,
                {
                    "flash": LlmAgent(
                        name="flash_node",
                        model=resolve_model("gemini-2.5-flash"),
                        instruction="Answer concisely.",
                    )
                },
            ),
        ],
    )
    bad_app = agent_engines.AdkApp(agent=bad)
    bad_app.set_up()
    bad_events, bad_chars = stream_chars(bad_app, message)
    results["misrouted_events"] = bad_events
    results["misrouted_chars"] = bad_chars
    results["misroute_is_silent_empty"] = bad_chars == 0
    return results


def render(r: dict) -> str:
    lines = [
        "=" * 70,
        "GRAPH-ROUTER SPIKE",
        "=" * 70,
        "",
        f"  Workflow is a BaseAgent      : {r['workflow_is_base_agent']}",
        f"  conditional route streams    : {r['streams']} "
        f"({r['routed_events']} events, {r['routed_chars']} chars)",
        f"  mis-emitted route -> empty   : {r['misroute_is_silent_empty']} "
        f"({r['misrouted_events']} events, {r['misrouted_chars']} chars)",
        "",
    ]
    if r["streams"] and r["misroute_is_silent_empty"]:
        lines += [
            "  The mechanism WORKS, and it has a new silent-empty failure mode:",
            "  a node that RETURNS its route key instead of YIELDING an Event with",
            "  EventActions(route=...) produces a 0-character stream and only a log",
            "  warning. That is empty-at-200 reachable through a new path.",
            "",
        ]
    if not r["workflow_is_base_agent"]:
        lines += [
            "  Workflow is NOT a BaseAgent (MRO: BaseNode -> BaseModel). AdkApp",
            "  duck-types it and set_up() succeeds, but this repo's root_agent",
            "  convention, _wants_memory(), engine_baseline and the agent-config",
            "  tests all assume BaseAgent — a broad blast radius for no new capability.",
            "",
        ]
    lines.append("=" * 70)
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message", default=DEFAULT_PROMPT)
    args = parser.parse_args(argv)
    results = probe(args.message)
    print(render(results))
    # Advisory: this is an investigation, not a gate.
    return 0


if __name__ == "__main__":
    sys.exit(main())
