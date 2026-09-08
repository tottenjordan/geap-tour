"""Write deployment outputs back into ``.env``, so config follows deploys.

``.env`` is the source of truth for every deployed identifier this repo uses
(``src/config.py`` reads it, ``verify_engine_config`` checks against it,
``find_orphan_engines`` reconciles with it). Anything a deploy produces and does
not write back has to be copy-pasted by hand, and a value nobody copies is a value
that silently goes stale — which is how a deleted engine stayed referenced for
3.5 months and how ``setup_apphub.sh`` ended up defaulting to two engines that no
longer existed.

Values are written **verbatim**. That matters: the previous writer lived inside
``deploy_agents`` and unconditionally applied ``value.split("/")[-1]`` to shorten a
resource name to its engine id — correct there, but it turns
``https://search-mcp-abc.run.app`` into ``search-mcp-abc.run.app``, silently
dropping the scheme. Shortening is a caller's decision, not the file writer's.
"""

from __future__ import annotations

import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENV_FILE = os.path.join(PROJECT_ROOT, ".env")


def set_env_var(name: str, value: str, *, path: str | None = None, quiet: bool = False) -> bool:
    """Set ``name=value`` in the env file. Returns True if the file changed.

    Rewrites the variable in place when present so surrounding comments, ordering
    and unrelated entries survive — a deploy must never reformat a file the user
    maintains by hand. Appends when absent, creates the file when missing.

    Already-correct values are a no-op, so re-running a deploy does not churn the
    file or print a misleading "updated" line.
    """
    target = path or ENV_FILE
    line = f"{name}={value}\n"

    lines: list[str] = []
    if os.path.exists(target):
        with open(target) as f:
            lines = f.readlines()

    for i, existing in enumerate(lines):
        # Match the assignment, not a prefix: FLASH_ENGINE_ID must not be matched
        # by a lookup for LASH_ENGINE_ID, and a commented-out line is not a match.
        if existing.split("=", 1)[0].strip() == name and not existing.lstrip().startswith("#"):
            if existing == line:
                return False
            lines[i] = line
            break
    else:
        # A file that does not end in a newline would otherwise splice the new
        # entry onto the last one.
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(line)

    with open(target, "w") as f:
        f.writelines(lines)
    if not quiet:
        print(f"  .env updated: {name}={value}")
    return True
