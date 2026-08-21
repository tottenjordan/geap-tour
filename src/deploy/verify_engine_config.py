"""Verify a deployed Agent Engine's SERVING config against the baseline.

The other verifiers in this repo check whether an engine *behaves* — its tools
resolve (``verify_mcp_tools``), it recalls across sessions
(``verify_cross_session_recall``), it does not stream empties
(``verify_router_health``). None of them check how it is **configured**, and
that is where the expensive failures have actually come from: a 4Gi container
that OOM-kills workers, a router whose tiers regressed to Gemini-3 on a plain
``--update``, an engine deployed before a fix and never redeployed.

Configuration drift is uniquely nasty because the engine keeps serving. There is
no error to grep for — the deploy succeeded, the health checks pass, and the
engine is quietly wrong until someone measures quality or reads a spec by hand.

This CLI closes that gap: it reads the live ``reasoningEngines`` spec and diffs
it against :mod:`src.deploy.engine_baseline`, which imports its expectations
from the deployer itself. Critical drift exits non-zero, so it works as a CI
step or a pre-demo gate.

Usage:
  uv run python -m src.deploy.verify_engine_config                     # .env engines
  uv run python -m src.deploy.verify_engine_config --engine-id <ID>
  uv run python -m src.deploy.verify_engine_config --engine-id <ID> --role router
  uv run python -m src.deploy.verify_engine_config --json
  uv run python -m src.deploy.verify_engine_config --why               # print rationale

Read-only: a GET against the Agent Engine control plane. It never deploys,
never mutates an engine, and never touches ``.env``.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from src.config import AGENT_ENGINE_ID, GCP_PROJECT_ID, GCP_REGION, ROUTER_ENGINE_ID
from src.deploy.engine_baseline import evaluate, has_critical_drift, infer_role

_API_VERSION = "v1beta1"


def _default_token() -> str:
    """ADC bearer token (mirrors src/eval/raw_stream.py's auth)."""
    import google.auth
    import google.auth.transport.requests as gart

    creds, _ = google.auth.default()
    creds.refresh(gart.Request())
    return creds.token


def _default_fetch(engine_id: str) -> dict:
    """GET the engine resource. Kept thin + injectable so tests skip the network."""
    import requests

    url = (
        f"https://{GCP_REGION}-aiplatform.googleapis.com/{_API_VERSION}"
        f"/projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/reasoningEngines/{engine_id}"
    )
    resp = requests.get(url, headers={"Authorization": f"Bearer {_default_token()}"}, timeout=60)
    resp.raise_for_status()
    return resp.json()


def normalize(resource: dict) -> dict[str, Any]:
    """Flatten the API resource into the shape :func:`evaluate` expects.

    The interesting fields sit at three different depths and the env arrives as
    a list of ``{name, value}`` pairs, so every caller would otherwise repeat
    this. Absent keys stay absent rather than becoming ``{}``/``0``, so a check
    can tell "unset" from "set to a falsy value".
    """
    spec = resource.get("spec") or {}
    deployment = spec.get("deploymentSpec") or {}
    return {
        "engine_id": (resource.get("name") or "").rsplit("/", 1)[-1],
        "display_name": resource.get("displayName"),
        "identity_type": spec.get("identityType"),
        "effective_identity": spec.get("effectiveIdentity"),
        "min_instances": deployment.get("minInstances"),
        "resource_limits": deployment.get("resourceLimits"),
        "labels": resource.get("labels") or {},
        "env": {v.get("name"): v.get("value") for v in (deployment.get("env") or [])},
        "update_time": resource.get("updateTime"),
    }


def check_engine(engine_id: str, role: str | None = None, *, fetch=None) -> dict[str, Any]:
    """Fetch one engine and evaluate it. Never raises — a fetch error is a result."""
    fetch = fetch or _default_fetch
    try:
        spec = normalize(fetch(engine_id))
    except Exception as exc:
        return {"engine_id": engine_id, "error": str(exc)[:300], "findings": [], "ok": False}
    findings = evaluate(spec, role)
    return {
        "engine_id": spec["engine_id"] or engine_id,
        "display_name": spec["display_name"],
        "role": role or infer_role(spec),
        "updated": (spec.get("update_time") or "")[:19],
        "findings": findings,
        "ok": not has_critical_drift(findings),
    }


def engine_exists(engine_id: str, *, fetch=None) -> bool:
    """True if ``engine_id`` resolves to a live engine on the control plane.

    Deleted engine ids linger in ``.env`` long after the engine is gone, and the
    consumers that read them (the cross-model experiment, the optimization report,
    the AppHub registration) build a resource *name* by string formatting — which
    always succeeds. The failure then surfaces deep inside an eval, or not at all.

    Any error (404, 403, network) reads as "not usable", which is the useful
    answer for a preflight: distinguishing gone from unreachable would not change
    the decision to stop.
    """
    if not engine_id:
        return False
    try:
        (fetch or _default_fetch)(engine_id)
    except Exception:
        return False
    return True


def default_targets() -> list[tuple[str, str]]:
    """The engines this repo actually serves from, as ``(engine_id, role)``.

    Sourced from config (``.env``) rather than by listing the project: the
    project is shared with other solutions, and listing would invite reporting
    on — or worse, acting on — engines that are not ours.
    """
    targets = [(AGENT_ENGINE_ID, "coordinator")]
    if ROUTER_ENGINE_ID and ROUTER_ENGINE_ID != AGENT_ENGINE_ID:
        targets.append((ROUTER_ENGINE_ID, "router"))
    return targets


def render(results: list[dict], *, show_why: bool = False) -> str:
    lines = ["=" * 74, "DEPLOYED ENGINE CONFIG", "=" * 74]
    for res in results:
        lines.append("")
        if res.get("error"):
            lines.append(f"  {res['engine_id']}  UNREACHABLE: {res['error']}")
            continue
        status = "PASS" if res["ok"] else "FAIL"
        lines.append(
            f"  {res['engine_id']}  {res.get('display_name') or ''} "
            f"[{res['role']}]  updated={res.get('updated')}"
        )
        lines.append(f"  config: {status}")
        for f in res["findings"]:
            mark = "ok " if f.ok else ("XX " if f.severity == "critical" else "!! ")
            lines.append(f"    {mark}{f.name:24} {f.observed}")
            if not f.ok:
                lines.append(f"       expected: {f.expected}")
            if show_why:
                lines.append(f"       why: {f.why}")
    crit = [f for r in results for f in r["findings"] if not f.ok and f.severity == "critical"]
    adv = [f for r in results for f in r["findings"] if not f.ok and f.severity == "advisory"]
    lines += ["", f"  {len(crit)} critical, {len(adv)} advisory"]
    if crit and not show_why:
        lines.append("  (re-run with --why for the rationale behind each check)")
    return "\n".join(lines)


def _jsonable(results: list[dict]) -> list[dict]:
    return [{**r, "findings": [f.as_dict() for f in r["findings"]]} for r in results]


def main(argv: list[str] | None = None, *, fetch=None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a deployed Agent Engine's serving config against the baseline."
    )
    parser.add_argument(
        "--engine-id",
        action="append",
        default=None,
        help="Engine to check (repeatable). Default: the coordinator + router from .env.",
    )
    parser.add_argument(
        "--role",
        default=None,
        choices=["coordinator", "router", "unknown"],
        help="Rule set to apply. Default: inferred from the engine's display name.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--why", action="store_true", help="Print the rationale for every check.")
    args = parser.parse_args(argv)

    if args.engine_id:
        targets: list[tuple[str, str | None]] = [(e, args.role) for e in args.engine_id]
    else:
        targets = [(eid, args.role or role) for eid, role in default_targets()]

    results = [check_engine(eid, role, fetch=fetch) for eid, role in targets]
    print(
        json.dumps(_jsonable(results), indent=2)
        if args.json
        else render(results, show_why=args.why)
    )
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
