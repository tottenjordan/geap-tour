"""Verify each MCP toolset actually resolves its expected tools.

The coordinator's MCP tools resolve lazily: ``src/registry.py:get_mcp_tools``
returns a toolset whose ``get_tools()`` opens the MCP session and enumerates
tools only at run time. When that fails — the Agent Registry can't resolve the
server, or the streamable-http session 404s ("Session terminated") — ADK logs a
warning and returns an **empty** tool list, and the agent proceeds tool-less.
That degradation is otherwise silent.

This CLI makes it loud. For each registered MCP server it resolves the toolset
through the exact ``get_mcp_tools`` path the coordinator uses and enumerates the
tools, then checks the real tool names are present. It prints ``MCP TOOLS:
PASS/FAIL`` per domain and exits non-zero if any toolset is empty or missing a
tool.

Usage:
  uv run python -m src.eval.verify_mcp_tools
  uv run python -m src.eval.verify_mcp_tools --json

Honest scope: run from the CLI it uses *your* ADC and the current
``*_MCP_SERVER`` / ``*_MCP_URL`` config — the same server config baked into the
deployed engine, but the engine authenticates as its own runtime SA. So a local
PASS confirms the servers + registry + config are healthy; the deployed engine's
own resolution path (registry vs. the now-loud direct-URL fallback) shows up in
its logs. Import-safe: no GCP/MCP work happens at import.
"""

import argparse
import json

from src.config import BOOKING_MCP_SERVER, EXPENSE_MCP_SERVER, SEARCH_MCP_SERVER

# The real tools each MCP server defines (src/mcp_servers/*/server.py). A toolset
# that resolves fewer than these has silently lost tools.
EXPECTED_TOOLS: dict[str, set[str]] = {
    "search": {"search_flights", "search_hotels"},
    "booking": {
        "book_flight",
        "book_hotel",
        "cancel_booking",
        "get_booking_details",
        "list_all_bookings",
    },
    "expense": {"submit_expense", "check_expense_policy", "get_user_expenses"},
}

# domain → registered Agent Registry resource name (from shared config).
SERVER_NAMES: dict[str, str] = {
    "search": SEARCH_MCP_SERVER,
    "booking": BOOKING_MCP_SERVER,
    "expense": EXPENSE_MCP_SERVER,
}


def evaluate_toolset(
    domain: str,
    resolved_tools,
    expected: dict[str, set[str]] = EXPECTED_TOOLS,
) -> dict:
    """Pure check: are all of ``domain``'s expected tools present in what resolved?

    A toolset is OK only when it resolved at least one tool AND every expected
    tool name is present (extra tools are fine).
    """
    exp = expected[domain]
    got = set(resolved_tools)
    missing = exp - got
    return {
        "domain": domain,
        "resolved": sorted(got),
        "missing": sorted(missing),
        "ok": bool(got) and not missing,
    }


def _enumerate_tools(server_name: str) -> list[str]:
    """Resolve a toolset via ``get_mcp_tools`` and return its live tool names.

    Imported lazily so this module stays import-safe without GCP/ADK MCP deps.
    """
    import asyncio

    from src.registry import get_mcp_tools

    async def _go() -> list[str]:
        toolset = get_mcp_tools(server_name)
        tools = await toolset.get_tools()
        return [name for t in tools if (name := getattr(t, "name", None))]

    return asyncio.run(_go())


def run_checks(*, server_names: dict[str, str] | None = None, enumerate_fn=None) -> list[dict]:
    """Check every configured MCP server; return one result dict per domain.

    ``enumerate_fn(server_name) -> list[str]`` and ``server_names`` are injectable
    for testing without live MCP connections.
    """
    server_names = server_names if server_names is not None else SERVER_NAMES
    enumerate_fn = enumerate_fn or _enumerate_tools

    results: list[dict] = []
    for domain, name in server_names.items():
        if not name:
            results.append(
                {
                    "domain": domain,
                    "resolved": [],
                    "missing": sorted(EXPECTED_TOOLS[domain]),
                    "ok": False,
                    "error": f"no server name configured ({domain.upper()}_MCP_SERVER unset)",
                }
            )
            continue
        try:
            tools = enumerate_fn(name)
        except Exception as exc:
            results.append(
                {
                    "domain": domain,
                    "resolved": [],
                    "missing": sorted(EXPECTED_TOOLS[domain]),
                    "ok": False,
                    "error": str(exc),
                }
            )
            continue
        results.append(evaluate_toolset(domain, tools))
    return results


def render(results: list[dict]) -> str:
    """Human-readable PASS/FAIL lines, one per domain plus an overall verdict."""
    lines: list[str] = []
    for r in results:
        status = "PASS" if r["ok"] else "FAIL"
        line = f"MCP TOOLS: {status} [{r['domain']}] resolved={r['resolved']}"
        if r.get("missing"):
            line += f" missing={r['missing']}"
        if r.get("error"):
            line += f" error={r['error']}"
        lines.append(line)
    overall = "PASS" if all(r["ok"] for r in results) else "FAIL"
    lines.append(f"MCP TOOLS: {overall} (overall)")
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, enumerate_fn=None, server_names=None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify each MCP toolset resolves its expected tools."
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON results.")
    args = parser.parse_args(argv)

    results = run_checks(server_names=server_names, enumerate_fn=enumerate_fn)
    print(json.dumps(results, indent=2) if args.json else render(results))
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
