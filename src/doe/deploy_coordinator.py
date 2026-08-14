"""Deploy ONE persistent coordinator engine for the bake-off, from its own process.

The bake-off compares two coordinators that differ only by ``COORDINATOR_MODEL``.
That variable is read once, at import time, in :mod:`src.config` and baked into the
engine's ``env_vars`` by :mod:`src.deploy.deploy_agents`. So the only way to deploy
two *different* backbones is a fresh interpreter per backbone with
``COORDINATOR_MODEL`` set in the environment — which is exactly how
:func:`src.doe.run_bakeoff._deploy_engine` invokes this module (subprocess-per-point,
the same pattern as :mod:`src.doe.launch`).

The deployed engine is **persistent** (``deploy_agent`` does not delete it and does
not touch ``.env``); the bake-off records its resource name in the run manifest and
tears it down at the end unless ``--keep-engines`` is passed. This CLI prints the
resource name on a :data:`RESOURCE_MARKER` line so the parent process can recover it
from stdout even amid the deploy's own chatty logging.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

# Prefix for the one stdout line that carries the deployed engine's resource name.
RESOURCE_MARKER = "BAKEOFF_ENGINE: "


def parse_resource_from_output(stdout: str) -> str | None:
    """Recover the deployed engine resource name from a subprocess's stdout.

    Returns the resource on the LAST :data:`RESOURCE_MARKER` line, or ``None`` when
    no marker is present (e.g. the deploy failed before printing it).
    """
    resource: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith(RESOURCE_MARKER):
            resource = line[len(RESOURCE_MARKER) :].strip()
    return resource


def main(
    argv: Sequence[str] | None = None,
    *,
    deploy_fn=None,
    update_fn=None,
    agent=None,
) -> int:
    """Deploy or update one coordinator engine; print its resource (marker line).

    ``deploy_fn`` / ``update_fn`` / ``agent`` are injectable so tests exercise the
    wiring without a real deploy; by default they bind the real ``deploy_agent`` /
    ``update_agent`` and the coordinator (imported lazily so a fresh subprocess
    picks up this point's ``COORDINATOR_MODEL``).

    With ``--update <engine-id>`` the engine is updated **in place** — a new
    revision of the same reasoningEngine via ``agent_engines.update`` — instead of
    creating a fresh one. This is how a persistent probe engine is iterated as
    revisions; ``update_agent`` never writes ``.env`` (only ``run_deploy`` does).
    """
    parser = argparse.ArgumentParser(description="Deploy or update one coordinator engine")
    parser.add_argument(
        "--display-name",
        default=None,
        help="Console display name for the deployed engine (e.g. per-backbone tag)",
    )
    parser.add_argument(
        "--update",
        default=None,
        metavar="ENGINE_ID",
        help="Update this existing engine in place (new revision) instead of creating one",
    )
    args = parser.parse_args(argv)

    if agent is None:
        from src.agents.coordinator_agent import coordinator_agent as agent

    if args.update:
        if update_fn is None:
            from src.deploy.deploy_agents import update_agent as update_fn
        resource = update_fn(agent, args.update, args.display_name)
    else:
        if deploy_fn is None:
            from src.deploy.deploy_agents import deploy_agent as deploy_fn
        resource = deploy_fn(agent, args.display_name)
    print(f"{RESOURCE_MARKER}{resource}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
