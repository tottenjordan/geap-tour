"""Find deployed engines of ours that nothing references any more.

`sonnet_agent` ``8467456143491334144`` was a deployment of this repo, abandoned on
2026-05-21 and deleted on 2026-09-08: **zero traffic for 30 days**, sitting on the
4Gi default that OOM-kills workers, superseded by ``sonnet_agent_jt1``. It survived
~3.5 months for one reason — nothing ever compared *what is deployed* against *what
is referenced*. It was found by accident, while chasing something else.

An abandoned engine is worse than idle. That one was on 4Gi, so anything that did
reach it would have OOM'd into an empty-at-200; and an engine nobody owns is an
engine nobody patches.

WHY THE OBVIOUS DESIGN WOULD NOT HAVE WORKED
--------------------------------------------
Reconciling *labelled-ours* against *config-referenced* is the natural approach and
it would have **missed the very engine that motivated this**: it had no label at
all. It predated ``RESOURCE_LABELS``. A detector that cannot catch its own founding
case is the exact "check that cannot detect its own failure" shape this repo keeps
finding (see docs/notes/checks-that-cannot-detect-their-own-failure.md).

So ownership is decided by an **env fingerprint** — the engine carries our Agent
Registry MCP resource names — which a deployment of this repo cannot lack, because
``src/registry.py`` needs them to resolve any tool at all. Measured over the live
project before this was committed to:

    labelled AND fingerprint   8   the current fleet, both signals agreeing
    labelled only              0
    FINGERPRINT ONLY           1   what a label-only detector misses
    neither                   30   other teams', correctly excluded

Zero false positives across 30 foreign engines, and the deleted `sonnet_agent`
carried those same vars — so this test would have caught it.

ON LISTING A SHARED PROJECT
---------------------------
``verify_engine_config.default_targets`` deliberately does **not** list the project:
"the project is shared with other solutions, and listing would invite reporting on —
or worse, acting on — engines that are not ours." That objection is correct and this
module has to earn its exception, so:

* it is **read-only**, and has no delete flag — a human deletes, once the ownership
  evidence is on screen;
* it **never names an engine that fails the fingerprint**. Foreign engines appear
  only in a count. ``tests/test_find_orphan_engines.py`` asserts that, because
  naming them is precisely the objection.

Usage:
  uv run python -m src.deploy.find_orphan_engines
  uv run python -m src.deploy.find_orphan_engines --json
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

from src.config import (
    BOOKING_MCP_SERVER,
    EXPENSE_MCP_SERVER,
    GCP_PROJECT_ID,
    GCP_REGION,
    LABEL_KEY,
    LABEL_VALUE,
    SEARCH_MCP_SERVER,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

_API_VERSION = "v1beta1"

# The env keys whose VALUES are matched against our registered MCP servers. These
# are project-and-location-scoped Agent Registry resource paths, so an engine
# holding one was deployed from this repo's config.
_FINGERPRINT_KEYS = ("SEARCH_MCP_SERVER", "BOOKING_MCP_SERVER", "EXPENSE_MCP_SERVER")

# Every config variable that can hold a deployed engine id. Hand-maintained, and
# therefore guarded: a new one added elsewhere and forgotten here would turn a LIVE
# engine into a reported orphan. `tests/test_find_orphan_engines.py` scans src/ for
# `*_ENGINE_ID` / `*_AGENT_ID` env reads and fails if this list does not cover them
# — otherwise the detector rots the same way its subject did.
ENGINE_ID_VARS = (
    "AGENT_ENGINE_ID",
    "COORDINATOR_AGENT_ID",
    "ROUTER_ENGINE_ID",
    "LITE_ENGINE_ID",
    "FLASH_ENGINE_ID",
    "PRO_ENGINE_ID",
    "SONNET_ENGINE_ID",
    "OPUS_ENGINE_ID",
)


# Engines that are OURS and deliberately referenced by no config variable. Without
# this the detector reports them every run, and a check that cries wolf on known-good
# state is one people learn to skim past — the failure it exists to prevent.
#
# Entries are shown in their own quiet section rather than hidden: an exception you
# cannot see is indistinguishable from a bug. Removing an engine from here must be a
# deliberate act, so each one carries the reason it is kept.
KNOWN_UNREFERENCED: dict[str, str] = {
    "4380288848559603712": (
        "demo coordinator probe — kept live on purpose and deliberately NOT in .env "
        "(pointing .env at it would repoint the served coordinator). Update it in "
        "place, never recreate."
    ),
}


def our_mcp_servers() -> set[str]:
    """The registry resource names that identify a deployment of this repo.

    Read at call time, not import time, so a test can point config elsewhere.
    Empty entries are dropped: an unset MCP var must not make every engine in the
    project match on the empty string.
    """
    return {s for s in (SEARCH_MCP_SERVER, BOOKING_MCP_SERVER, EXPENSE_MCP_SERVER) if s}


def is_ours(spec: dict) -> bool:
    """True when the engine's env carries one of our MCP registry resource names.

    Deliberately NOT the label. The engine this module exists for had none.
    """
    ours = our_mcp_servers()
    if not ours:
        return False
    env = spec.get("env") or {}
    return any(env.get(k) in ours for k in _FINGERPRINT_KEYS)


def is_labelled(spec: dict) -> bool:
    """True when the engine carries our `solution=geap-tour` resource label."""
    return (spec.get("labels") or {}).get(LABEL_KEY) == LABEL_VALUE


def referenced_engine_ids() -> dict[str, str]:
    """``{engine_id: the env var that names it}`` for every id config points at."""
    out: dict[str, str] = {}
    for var in ENGINE_ID_VARS:
        value = (os.environ.get(var) or "").strip()
        if value:
            out.setdefault(value.rsplit("/", 1)[-1], var)
    return out


def find_orphans(specs: Iterable[dict], referenced: dict[str, str]) -> dict[str, Any]:
    """The whole verdict, pure. ``specs`` are :func:`normalize`-shaped dicts.

    Three findings are reported **separately** rather than as one "problem" list,
    because conflating them is how the last orphan hid: an unlabelled engine and an
    unreferenced engine need different fixes, and a dangling config id is not an
    engine problem at all.
    """
    specs = list(specs)
    ours = [s for s in specs if is_ours(s)]
    seen = {s.get("engine_id") for s in ours}

    orphans = [
        {
            "engine_id": s.get("engine_id"),
            "display_name": s.get("display_name"),
            # The triage facts. "Is it costing anything, and is it a trap?" is the
            # first question — the last orphan was 4Gi, which OOMs into empty-at-200.
            "min_instances": s.get("min_instances"),
            "memory": (s.get("resource_limits") or {}).get("memory"),
            "update_time": s.get("update_time"),
            "labelled": is_labelled(s),
        }
        for s in ours
        if s.get("engine_id") not in referenced and s.get("engine_id") not in KNOWN_UNREFERENCED
    ]
    kept = [
        {
            "engine_id": s.get("engine_id"),
            "display_name": s.get("display_name"),
            "reason": KNOWN_UNREFERENCED[s["engine_id"]],
        }
        for s in ours
        if s.get("engine_id") in KNOWN_UNREFERENCED
    ]
    unlabelled = [
        {"engine_id": s.get("engine_id"), "display_name": s.get("display_name")}
        for s in ours
        if not is_labelled(s)
    ]
    dangling = [
        {"engine_id": eid, "env_var": var} for eid, var in referenced.items() if eid not in seen
    ]

    return {
        # Counts only for anything that is not ours — see the module docstring.
        "total_engines": len(specs),
        "ours": len(ours),
        "orphans": sorted(orphans, key=lambda o: o["engine_id"] or ""),
        "kept": sorted(kept, key=lambda o: o["engine_id"] or ""),
        "unlabelled": sorted(unlabelled, key=lambda o: o["engine_id"] or ""),
        "dangling": sorted(dangling, key=lambda o: o["engine_id"] or ""),
    }


def _default_list_engines() -> list[dict]:
    """List the project's engines and normalize them. Injected in tests."""
    import requests

    from src.auth import adc_bearer_token
    from src.deploy.verify_engine_config import normalize

    url = (
        f"https://{GCP_REGION}-aiplatform.googleapis.com/{_API_VERSION}"
        f"/projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/reasoningEngines?pageSize=100"
    )
    out: list[dict] = []
    token = adc_bearer_token()
    while url:
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=90)
        resp.raise_for_status()
        body = resp.json()
        out.extend(normalize(e) for e in body.get("reasoningEngines") or [])
        page = body.get("nextPageToken")
        url = f"{url.split('&pageToken=')[0]}&pageToken={page}" if page else ""
    return out


def render(result: dict) -> str:
    """Human-readable report. Never names an engine that is not ours."""
    lines = [
        "=" * 74,
        "ORPHANED ENGINES",
        "=" * 74,
        "",
        f"  {result['total_engines']} engines in project, {result['ours']} ours "
        "(by MCP-registry fingerprint, not label)",
        "",
    ]
    if result["orphans"]:
        lines.append("  ORPHANS — ours, but no config variable references them:")
        for o in result["orphans"]:
            warm = f"min={o['min_instances'] or 0}"
            mem = o["memory"] or "4Gi (platform default — OOMs workers)"
            tag = "" if o["labelled"] else "  [unlabelled]"
            lines.append(
                f"    {o['engine_id']}  {o['display_name'] or '?':28} "
                f"{warm}  mem={mem}  updated={(o['update_time'] or '')[:10]}{tag}"
            )
        lines.append("")
    if result["unlabelled"]:
        lines.append("  UNLABELLED — ours by fingerprint, missing solution=geap-tour:")
        lines += [f"    {u['engine_id']}  {u['display_name'] or '?'}" for u in result["unlabelled"]]
        lines.append("")
    if result["dangling"]:
        lines.append("  DANGLING — config names an engine that does not exist:")
        lines += [f"    {d['env_var']}={d['engine_id']}" for d in result["dangling"]]
        lines.append("")
    if result.get("kept"):
        lines.append("  KEPT — ours, unreferenced ON PURPOSE (see KNOWN_UNREFERENCED):")
        for k in result["kept"]:
            lines.append(f"    {k['engine_id']}  {k['display_name'] or '?'}")
            lines.append(f"      {k['reason']}")
        lines.append("")
    if not (result["orphans"] or result["unlabelled"] or result["dangling"]):
        lines.append("  No orphans, no unlabelled engines, no dangling references.")
        lines.append("")
    lines += [
        "  Read-only: nothing is deleted here. Confirm ownership, then delete by hand.",
        "=" * 74,
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, list_engines=None) -> int:
    """CLI. Always exits 0 — advisory, like the monitoring step that calls it."""
    import argparse

    parser = argparse.ArgumentParser(description="Report deployed engines nothing references.")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    specs = (list_engines or _default_list_engines)()
    result = find_orphans(specs, referenced_engine_ids())
    print(json.dumps(result, indent=2, sort_keys=True) if args.json else render(result))
    # Advisory by design: an engine can be legitimately unreferenced for days (a
    # bake-off deploy, an in-flight experiment). Going red for normal work is how a
    # signal gets ignored.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
