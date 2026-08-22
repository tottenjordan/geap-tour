"""Version + checksum every committed evalset, so a dataset can't change silently.

An eval score only means something relative to the dataset that produced it. This
repo's evalsets are plain JSON that anyone can edit, and nothing recorded that they
had — so a score moving between two runs was ambiguous: the agent changed, or the
questions did. That ambiguity is what makes a "regression set" not actually frozen.

This module records, per evalset, a `version`, a content `checksum`, and the case
count in a committed manifest. :func:`verify` recomputes and reports drift;
`tests/test_dataset_manifest.py` fails the build on it. Changing a dataset is
therefore still easy — you just have to *say so*:

    uv run python -m src.eval.dataset_manifest --check    # what drifted?
    uv run python -m src.eval.dataset_manifest --update   # accept + bump

Two families are tracked, and the distinction is the point (roadmap P2.7):

* **regression** — ``src/eval/evalsets/*.evalset.json``, what the offline eval
  grades. These should essentially never change; a bump here invalidates
  comparisons against every previously published score.
* **development** — ``src/agents/*/*.evalset.json`` + the router's, what GEPA
  optimizes against. These are *expected* to evolve, but a change still has to be
  acknowledged, because an unnoticed edit here can quietly contaminate the split
  that :mod:`src.eval.holdout` enforces.

The checksum is over the **canonical JSON of the eval cases**, not the raw bytes:
reformatting, re-indenting or reordering keys does not churn it, while any change
to a prompt, a reference, or an expected tool does. Case *ordering* is deliberately
significant — for a set that claims to be frozen, "the same cases in a different
order" is a change worth one line of acknowledgement.

Pure and offline: reads committed JSON, no GCP, no SDK.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from src.eval.dataset_integrity import EVAL_EVALSETS, TRAIN_EVALSETS, resolve

MANIFEST_PATH = "src/eval/data/dataset_manifest.json"

# In-code case lists that are NOT evalset JSON but still feed graded output — and
# in the router's case a *monitored, alerting* series. `ROUTER_EVAL_CASES` drives
# `agent_router/routing_accuracy_pct`, so editing it silently moves a published
# metric; CLAUDE.md warns about this and nothing enforced it. Keyed by a
# `python:` pseudo-path so the manifest can hold both kinds without ambiguity.
CODE_CASE_LISTS: dict[str, str] = {
    "python:src.eval.agent_eval_configs.ROUTER_EVAL_CASES": "regression",
    "python:src.eval.agent_eval_configs.TRAVEL_EVAL_CASES": "regression",
    "python:src.eval.agent_eval_configs.EXPENSE_EVAL_CASES": "regression",
}

# Repo-relative evalset path -> role. Built from the two families dataset_integrity
# already defines, so a new evalset registered there is tracked here automatically.
TRACKED: dict[str, str] = {
    **dict.fromkeys(EVAL_EVALSETS.values(), "regression"),
    **dict.fromkeys(TRAIN_EVALSETS.values(), "development"),
    **CODE_CASE_LISTS,
}

_INITIAL_VERSION = "1.0.0"


def _import_cases(spec: str) -> list:
    """Resolve a ``python:module.ATTR`` pseudo-path to its case list."""
    from importlib import import_module

    module_path, _, attr = spec.removeprefix("python:").rpartition(".")
    return list(getattr(import_module(module_path), attr))


def _cases(path: str | Path) -> list:
    if isinstance(path, str) and path.startswith("python:"):
        return _import_cases(path)
    data = json.loads(resolve(path).read_text())
    return data.get("eval_cases") or data.get("evalCases") or []


def checksum(path: str | Path) -> str:
    """sha256 over the canonical JSON of the evalset's cases (formatting-stable)."""
    canonical = json.dumps(_cases(path), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def describe(path: str | Path) -> dict:
    """Current on-disk facts for one evalset."""
    return {"checksum": checksum(path), "n_cases": len(_cases(path))}


def build_manifest(previous: dict | None = None) -> dict:
    """Recompute the manifest, preserving each dataset's declared ``version``.

    Versions are **not** auto-bumped: a bump is a human statement that the change
    was intended, and inventing one would defeat the purpose of the check.
    """
    prior = (previous or {}).get("datasets", {})
    datasets = {}
    for path, role in sorted(TRACKED.items()):
        datasets[path] = {
            "role": role,
            "version": prior.get(path, {}).get("version", _INITIAL_VERSION),
            **describe(path),
        }
    return {"datasets": datasets}


def load_manifest(path: str | Path = MANIFEST_PATH) -> dict:
    resolved = resolve(path)
    if not resolved.exists():
        return {"datasets": {}}
    return json.loads(resolved.read_text())


def verify(manifest: dict | None = None) -> list[str]:
    """Return human-readable drift lines; empty means every evalset matches."""
    recorded = (manifest if manifest is not None else load_manifest()).get("datasets", {})
    problems = []
    for path, role in sorted(TRACKED.items()):
        entry = recorded.get(path)
        if entry is None:
            problems.append(f"{path}: tracked as '{role}' but absent from the manifest")
            continue
        actual = describe(path)
        if actual["checksum"] != entry.get("checksum"):
            problems.append(
                f"{path}: content changed without a version bump "
                f"(recorded v{entry.get('version')}, {entry.get('n_cases')} cases; "
                f"now {actual['n_cases']} cases)"
            )
    for path in sorted(set(recorded) - set(TRACKED)):
        problems.append(f"{path}: in the manifest but no longer tracked — remove it")
    return problems


def write_manifest(path: str | Path = MANIFEST_PATH) -> dict:
    """Recompute and write the manifest. Run after an intentional dataset change."""
    manifest = build_manifest(load_manifest(path))
    target = resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify or refresh the evalset manifest.")
    parser.add_argument("--check", action="store_true", help="Report drift (default).")
    parser.add_argument(
        "--update",
        action="store_true",
        help="Recompute checksums and write the manifest (bump versions by hand).",
    )
    args = parser.parse_args(argv)

    if args.update:
        manifest = write_manifest()
        print(f"Wrote {MANIFEST_PATH} ({len(manifest['datasets'])} datasets)")
        print("Bump the `version` of any dataset you changed on purpose.")
        return 0

    problems = verify()
    if not problems:
        print(f"Datasets OK — {len(TRACKED)} evalsets match the manifest.")
        return 0
    print("Dataset drift:")
    for line in problems:
        print(f"  {line}")
    print("\nIf intended: bump the dataset's `version`, then run --update.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
