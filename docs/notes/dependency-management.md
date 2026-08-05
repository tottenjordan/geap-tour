# Dependency management & the internal registry gotcha

_Written 2026-08-05. Re-verify commands/paths before acting._

## The gotcha

The `refactor/chasin-evals` branch's original `uv.lock` sourced **every package
(~2385 entries)** from an **internal Google "Artifact Foundry" mirror**:

```
https://us-python.pkg.dev/artifact-foundry-prod/ah-3p-staging-python/simple/
```

This registry is **not accessible** from this environment (account
`admin@jordantotten.altostrat.com`):

- Unauthenticated fetch → **401**.
- Authenticated with `gcloud auth print-access-token` → **403 Forbidden**.
- `gcloud artifacts repositories describe ah-3p-staging-python
  --project=artifact-foundry-prod --location=us` → `PERMISSION_DENIED`
  (`artifactregistry.repositories.get`). The project isn't ours; access can't be
  self-granted.

Implication: that lockfile was generated in an environment with internal access
(original author's machine / internal CI). It is likely **un-installable in
public GitHub CI** too.

## What this means for `uv lock` here

There is **no `[tool.uv]` index config in `pyproject.toml`** and no `UV_INDEX*`
env vars — the internal mirror came purely from the lock-time environment. So
running `uv lock` in this environment re-resolves everything from **public
PyPI** (`pypi.org/simple`). Since all these are public packages (`a2a-sdk`,
`numpy`, `ruff`, …), the PyPI lock is functionally equivalent and **portable**.

Decision taken (2026-08-05, PR #2): commit the **PyPI-sourced** lock. If the
team later wants to keep the internal mirror, re-run `uv lock` from an
internal-access environment.

## Practical rules

- `uv sync`/`uv lock` work here **only** against PyPI. Don't expect the internal
  mirror to resolve.
- If a future lock diff flips ~thousands of `source = ...` lines between
  `us-python.pkg.dev/...` and `pypi.org/simple`, that's this same environment
  mismatch — not a real dependency change.
- Package management still follows [CODE_STANDARDS.md](../../CODE_STANDARDS.md):
  `uv add` / `uv sync` / `uv run`, never bare pip.
