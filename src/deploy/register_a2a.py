"""Register / discover the coordinator's A2A agent card in Agent Registry.

PREVIEW-OPTIONAL CLI. Registering (default) publishes the coordinator's agent
card; ``--discover`` lists A2A agents already in the registry. On any
error — the A2A/registry preview surface being unavailable, missing
credentials, etc. — this logs a clear "A2A preview not enabled — skipping"
notice and exits 0, so it never fails a live demo.

Usage:
    uv run python -m src.deploy.register_a2a            # register (default)
    uv run python -m src.deploy.register_a2a --register # explicit register
    uv run python -m src.deploy.register_a2a --discover # list A2A agents
"""

import argparse
import logging
import sys

from src.a2a.agent_card import build_agent_card
from src.registry import A2A_PREVIEW_SKIP, get_a2a_agents, register_a2a_agent

log = logging.getLogger("register_a2a")


def _register() -> None:
    card = build_agent_card()
    log.info("Registering A2A agent card for %r ...", card.name)
    result = register_a2a_agent(card)
    if not result:
        # register_a2a_agent already logged the specific reason.
        log.info("%s (agent card not registered)", A2A_PREVIEW_SKIP)
        return
    log.info("Registered A2A agent: %s", result.get("name", "<unknown>"))


def _discover() -> None:
    agents = get_a2a_agents()
    if not agents:
        log.info("%s (no A2A agents discovered)", A2A_PREVIEW_SKIP)
        return
    log.info("Discovered %d A2A agent(s):", len(agents))
    for agent in agents:
        print(f"  {agent.get('name', '<unknown>')} — {agent.get('displayName', '')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--register",
        action="store_true",
        help="Register the coordinator's agent card (default).",
    )
    group.add_argument(
        "--discover",
        action="store_true",
        help="List A2A agents registered in Agent Registry.",
    )
    args = parser.parse_args(argv)

    try:
        if args.discover:
            _discover()
        else:
            _register()
    except Exception as exc:  # belt-and-suspenders: helpers already guard
        log.info("%s (%s)", A2A_PREVIEW_SKIP, exc)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
