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

## What is deliberately held back (2026-09-08 refresh)

A full refresh took 17 of 30 direct dependencies to current. The remaining **six are
blocked upstream, not skipped** — each has a named blocker, recorded so the next
refresh does not re-derive the same dead ends:

| Held at | Blocked by |
| --- | --- |
| `fastmcp` 3.4.7 (latest 4.0.3) | 4.x requires `mcp` 2.x; **ADK cannot import against mcp 2.x** — `ModuleNotFoundError: No module named 'mcp.shared.session'`. Now pinned `<4` for the same reason `mcp` is pinned `<2`. |
| `litellm` 1.96.2 (latest 1.100.0) | `google-cloud-aiplatform[evaluation]` requires `litellm>=1.93.0,<1.97.0`. Confirmed on **both** 1.165.1 and 2.1.0, so it is a durable cap, not a 1.x artifact. |
| `opentelemetry-exporter-otlp-proto-grpc` and the three `opentelemetry-instrumentation-*` | ADK pins `opentelemetry-sdk<=1.42.1`. ADK 2.7.1 shipped specifically to *restore* that ceiling, so it is intentional upstream. |

**Two rules this refresh earned.**

*An open upper bound on a package with a transitive runtime coupling is a trap.*
`fastmcp>=3.4.7` looked harmless and would have silently pulled mcp 2.x — the same
break that once took down every deploy. Where a dependency's major version dictates a
sibling's, pin the major.

*A green suite does not prove an SDK bump worked.* The `google-cloud-aiplatform`
1.x → 2.x bump passed 1628 tests and a full import spike while **every real eval
returned nothing** — a changed function signature and a removed `Client.agent_engines`
attribute, neither visible to an import check. Any bump of `google-adk` or
`google-cloud-aiplatform` must be followed by one real scored run:

```bash
uv run python -m src.eval.multi_agent_batch_eval --agents coordinator_agent \
  --agent-id <PROBE_ENGINE_ID> --limit 4     # must return NON-ZERO metrics
uv run python -m src.eval.verify_memory --user-id alice --engine-id <PROBE_ENGINE_ID>
uv run python -m src.eval.verify_mcp_tools --json
```
