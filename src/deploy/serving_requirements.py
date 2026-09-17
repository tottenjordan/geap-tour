"""Render the served-engine dependency set to the `requirements.txt` files.

There are three places a serving dependency set is declared in this repo, and until
2026-09-17 all three were maintained by hand and had drifted apart:

* ``src/deploy/deploy_agents.py:REQUIREMENTS`` — used by the Python deployer, the
  path ``deploy_all.sh`` and every documented command actually take. Curated, with
  the reasoning for each bound written next to it.
* ``src/router/requirements.txt`` — passed to ``adk deploy agent_engine
  --requirements_file`` by ``scripts/deploy_router.sh``.
* ``src/agents/coordinator/requirements.txt`` — picked up by the ADK CLI's agent-dir
  convention, and shown in ``docs/workshop_guide.md``.

The drift was not cosmetic. Both ``.txt`` copies carried ``google-adk[agent-identity]>=2``
(defeating the exact pin the cloudpickle contract depends on), no ``mcp`` bound at all,
and the coordinator's carried ``fastmcp>=2.0.0`` — which resolves to fastmcp 4.x, which
requires mcp 2.x, which removed the ``McpHttpClientFactory`` symbol ADK imports. That is
the exact dependency chain that killed every worker at import on 2026-09-08, sitting
armed in a file a live deploy script reads.

So the ``.txt`` files stop being sources. They are generated from ``REQUIREMENTS`` and
committed (kept on disk, because the ADK CLI discovers them by convention and the
workshop guide points at them), and ``--check`` fails if they drift again.

Usage::

    uv run python -m src.deploy.serving_requirements --check   # CI / tests
    uv run python -m src.deploy.serving_requirements --write   # regenerate
"""

from __future__ import annotations

import argparse
import pathlib

from src.deploy.deploy_agents import REQUIREMENTS

#: Generated copies, relative to the repo root. Both get identical content: the
#: served engine is the same runtime whichever agent it hosts, so a per-agent subset
#: would only be another thing to get wrong.
GENERATED = (
    "src/router/requirements.txt",
    "src/agents/coordinator/requirements.txt",
)

HEADER = """\
# GENERATED FILE — DO NOT EDIT.
#
# Source of truth: src/deploy/deploy_agents.py:REQUIREMENTS, which carries the
# reasoning for every bound below. Regenerate with:
#
#     uv run python -m src.deploy.serving_requirements --write
#
# Hand-editing this file silently diverges the `adk deploy agent_engine` path from
# the Python deployer. It already did once: this file held `google-adk>=2` and
# `fastmcp>=2.0.0`, which resolves fastmcp 4.x -> mcp 2.x -> the import error that
# killed every engine worker on 2026-09-08.
"""


def render() -> str:
    """The exact bytes every generated serving requirements file should contain."""
    return HEADER + "\n" + "\n".join(REQUIREMENTS) + "\n"


def _root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def check() -> list[str]:
    """Paths whose content differs from :func:`render` (missing counts as drift)."""
    want = render()
    root = _root()
    return [rel for rel in GENERATED if not (p := root / rel).is_file() or p.read_text() != want]


def write() -> list[str]:
    """Regenerate every managed file; returns the ones that actually changed."""
    want = render()
    root = _root()
    changed = []
    for rel in GENERATED:
        path = root / rel
        if not path.is_file() or path.read_text() != want:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(want)
            changed.append(rel)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="Fail if any copy drifted.")
    group.add_argument("--write", action="store_true", help="Regenerate the copies.")
    args = parser.parse_args(argv)

    if args.check:
        if drifted := check():
            print("Serving requirements are stale — regenerate with --write:")
            for rel in drifted:
                print(f"  {rel}")
            return 1
        print(f"All {len(GENERATED)} serving requirements files match REQUIREMENTS.")
        return 0

    if changed := write():
        for rel in changed:
            print(f"wrote {rel}")
    else:
        print("Already up to date.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
