# Agent Registry MCP resolution — two failure surfaces, and what's actually fixable

**What this is (2026-08-15):** the deployed coordinator logged two warnings on
every run. Investigation established they are **two independent failure surfaces**,
and that only one of them is a repo bug. This note records the diagnosis, the fix,
and the honest limits.

The two log lines:

1. `Agent Registry unavailable for projects/hybrid-vertex/locations/us-central1/mcpServers/agentregistry-00000000-0000-0000-1089-2fb19b9297d7` (→ `using direct URL …`)
2. `Failed to get tools from toolset AgentRegistrySingleMcpToolset: Failed to get tools from MCP server: Session terminated`

## Surface A — registry resolution fails → direct-URL fallback

`src/registry.py:get_mcp_tools(server_name)` calls
`AgentRegistry.get_mcp_toolset(name)`, which synchronously hits the Agent Registry
*control plane* (`agentregistry.googleapis.com/v1`, alpha surface). On any failure
it raises, and we fall back to the direct Cloud Run URL from `MCP_SERVER_URLS`.

**This is what "the agent uses local instead of the registry" means** — and inside
the managed Agent Engine runtime it happens **on every request, for all three
servers**. The true root cause is a **wrong-principal IAM denial**, not a repo-config
error and not (as first hypothesized) an unreachable API. The exact runtime error,
surfaced only after the loud-fallback change logged the swallowed exception, is:

```
API request failed with status 403:
  "status": "PERMISSION_DENIED", "reason": "IAM_PERMISSION_DENIED",
  "message": "Permission 'agentregistry.mcpServers.get' denied on resource
             '//agentregistry.googleapis.com/…/mcpServers/agentregistry-00000000-…'"
```

So the runtime **does** reach `agentregistry.googleapis.com` — it gets a genuine
`IAM_PERMISSION_DENIED`. What we ruled out and what we found:

- **The `.env` resource names are live and correct.** The
  `agentregistry-00000000-0000-0000-{4bce…,f126…,1089…}` names (the `00000000-…`
  prefix is the genuine system-assigned ID format, not a placeholder) resolve to
  HTTP 200 with the right Cloud Run endpoints from admin creds.
- **The RE *service agent* already has read** — `service-934903580331@gcp-sa-aiplatform-re`
  holds `roles/editor` (includes `agentregistry.mcpServers.get`), `aiplatform.user`,
  `mcp.toolUser`, `cloudapiregistry.viewer`, and (granted during this investigation)
  `roles/agentregistry.viewer`. **But the deployed engine does not run as this SA.**
- **The engine runs under a per-engine Agent Identity.** Its spec has
  `identityType: AGENT_IDENTITY` and `serviceAccount: <none>`; its
  `effectiveIdentity` is
  `agents.global.org-595744329948.system.id.goog/resources/aiplatform/projects/934903580331/locations/us-central1/reasoningEngines/<engine-id>`
  — a distinct workload-identity principal, **unique per engine**. Every IAM role
  we granted went to the RE service agent, which the runtime never uses. That is
  precisely why the grants had no effect and the 403 persisted on a fresh throwaway
  (engine `4892010356219576320`, us-central1).

**Conclusion (corrected):** registry-primary in the managed runtime is **achievable
and was achieved** — the engine's per-engine Agent Identity needed
`agentregistry.mcpServers.get` (e.g. `roles/agentregistry.viewer`). This is a
different, newer identity model than the shared RE service agent, so the fix was a new
IAM grant, not a config change. Two remediation paths existed:
1. **Grant the Agent Identity (chosen, applied).** Bind `roles/agentregistry.viewer`
   to the engine's agent-identity principal. The IAM member is
   `principal://<effectiveIdentity>` (i.e. `principal://agents.global.org-<ORG>.system.id.goog/resources/aiplatform/projects/<PN>/locations/<REGION>/reasoningEngines/<ENGINE_ID>`).
   Because the identity is per-engine, this is a per-engine binding (re-grant on
   *recreate*, though not on in-place `--update`); a project/org-level `principalSet`
   over the `…system.id.goog` pool is the broader-blast-radius alternative (admin
   decision, not used). Reproducible in `scripts/setup_governance_policies.sh`
   ("Step 0b").
2. **Deploy with a custom service account (fallback, not needed).** Pass a
   `service_account` (with `agentregistry.viewer` + `aiplatform.user`) to
   `agent_engines.create`, which switches the engine off `AGENT_IDENTITY` onto a
   shared SA identity — trading away per-engine isolation. Requires a
   `deploy_agents.py` change + SA management.

**What was actually done (2026-08-15).** Path 1. Validated on a throwaway coordinator
first: pre-grant it logged `403 IAM_PERMISSION_DENIED → direct-URL fallback`; after
granting `roles/agentregistry.viewer` to its `principal://…/reasoningEngines/<id>`,
a fresh resolution returned `AgentRegistrySingleMcpToolset` (the registry toolset)
with HTTP-200 tool calls and zero fallback. Then the same grant was applied to the
pinned coordinator `3639024497392091136`. **Caching gotcha:** an already-running engine
resolves each toolset **once per container instance** and reuses it — the pinned
engine's live container had cached the fallback (resolved during the IAM-propagation
window), so the cutover only completed after an in-place `deploy_agents coordinator
--update` recycled its containers (which also re-baked the loud-fallback code below).
Post-update: zero fallback WARNINGs under the new code + successful MCP tool calls =
registry path. `ENABLE_AGENT_IDENTITY=true` in `.env` is what puts engines on the
per-engine identity in the first place.

**So the direct-URL fallback is the load-bearing path in the deployed engine, and
that is fine** — the coordinator executes real MCP tool calls over it (a
`Find a flight SFO→JFK` probe streamed `function_call → function_response → text`
with real results). What was wrong was that the fallback was **silent** (logged at
`INFO`, and the swallowed exception detail was discarded). We made it loud:

- `get_mcp_tools` now catches `(RuntimeError, ValueError)` — ADK raises
  `RuntimeError` on control-plane HTTP/creds errors **and** `ValueError` when a
  resolved entry has no endpoint URI; the old `except RuntimeError` would have
  crashed on the latter instead of falling back.
- The fallback logs at **WARNING** and includes the underlying exception, so an
  operator sees *why* it fell back, not just that it did.
- The GEPA sandbox copy (`src/agents/coordinator/agent.py`) was kept in sync.

## Surface B — "Session terminated" → tool-less coordinator

When resolution *succeeds*, the toolset opens no MCP session until run time;
`get_tools()` then does a streamable-http `list_tools()` POST. If the Cloud Run
instance that created the `Mcp-Session-Id` was replaced/scaled between calls, the
POST returns **HTTP 404**, which the MCP client maps to
`ErrorData(code=32600, "Session terminated")`. ADK retries once, then **swallows it
and returns `[]`** — the agent proceeds tool-less, silently. This hits the registry
and direct-URL paths equally (both terminate at the same Cloud Run endpoint).

Root cause: FastMCP `streamable-http` keeps **per-instance in-memory session state**,
and Cloud Run autoscaling (scale-to-zero, `maxScale` 100, no session affinity) drops
those sessions.

**Fix — make the servers stateless.** FastMCP 3.4.7 forwards
`run(transport="streamable-http", …, stateless_http=True)` → `run_http_async` →
`http_app(stateless_http=True)`. Stateless mode drops the `Mcp-Session-Id` binding,
so **any** instance can serve **any** POST and the 404 cannot occur regardless of
scaling. Set in all three of `src/mcp_servers/{search,booking,expense}/server.py`.
As latency insurance (not correctness), `src/deploy/deploy_mcp_servers.py` also adds
`--min-instances 1` so a cold start can't exceed the coordinator's 60s MCP connect
timeout — but `--max-instances 1` / `--session-affinity` are deliberately **not**
used: stateless HTTP already removes the cross-instance dependency, and pinning to
one instance would cap throughput for no benefit.

Note: "Session terminated" was **not** present in the deployed engine's logs over
the observed window (the fallback path was healthy), so this is a fixed **latent**
bug, mechanically confirmed rather than observed firing.

## Detecting the silent degradation going forward

`src/eval/verify_mcp_tools.py` resolves each MCP server through the exact
`get_mcp_tools` path and enumerates its tools, asserting the real tool names are
present (`search_flights`/`search_hotels`, `book_flight`/…, `check_expense_policy`/
`submit_expense`/`get_user_expenses`). Prints `MCP TOOLS: PASS/FAIL` per domain and
exits non-zero on any empty/missing toolset — turning the previously-silent `[]`
into a detectable signal. Honest scope: run from the CLI it uses *your* ADC (not the
runtime SA), so a PASS confirms the servers + registry + config are healthy; the
deployed engine's own resolution path shows up in its (now loud) logs.

## Diagnostic commands

```bash
# Real registry entries + endpoints (admin creds succeed even though the runtime can't)
gcloud alpha agent-registry services list --project=hybrid-vertex --location=us-central1

# Runtime SA's roles
gcloud projects get-iam-policy hybrid-vertex --flatten="bindings[].members" \
  --filter="bindings.members:gcp-sa-aiplatform-re" --format="value(bindings.role)"

# Which surface is firing in a deployed engine's logs (the loud fallback now
# carries the underlying 403/exception text — that is how the root cause surfaced)
gcloud logging read \
  'resource.type="aiplatform.googleapis.com/ReasoningEngine" AND resource.labels.reasoning_engine_id="<ID>" AND (textPayload:"resolution failed" OR textPayload:"Session terminated" OR textPayload:"IAM_PERMISSION_DENIED")' \
  --project=hybrid-vertex --freshness=20m --format="value(timestamp, textPayload)"

# The identity the engine ACTUALLY runs as (AGENT_IDENTITY ≠ the RE service agent)
TOKEN=$(gcloud auth print-access-token)
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://us-central1-aiplatform.googleapis.com/v1/projects/hybrid-vertex/locations/us-central1/reasoningEngines/<ID>" \
  | python3 -c "import sys,json;s=json.load(sys.stdin)['spec'];print(s.get('identityType'),s.get('effectiveIdentity'),s.get('serviceAccount','<none>'))"

# Enumerate each toolset's live tools (PASS/FAIL)
uv run python -m src.eval.verify_mcp_tools --json
```

## Caveats

- **Single project, alpha surface.** Agent Registry is `gcloud alpha`; the
  Agent-Identity model and resource-name format may shift. Re-verify before assuming.
- **The RE-service-agent grant was a red herring** (the runtime uses AGENT_IDENTITY),
  but it's harmless and documents the ruled-out hypothesis. Registry-primary is a live
  IAM decision on a *new* principal type — Path 1 above was applied (see "What was
  actually done").
- **Per-engine binding, not project-wide.** The grant targets one engine's
  `effectiveIdentity`. A **recreate** (not `--update`) mints a new engine identity and
  needs a fresh grant — hence the reproducible "Step 0b" helper in
  `setup_governance_policies.sh` covers the pinned coordinator + router by id. The
  router's agent identity is **wired in the script but was not granted live** this pass
  (coordinator-only scope).
- **Live-infra changes made:** granted `roles/agentregistry.viewer` to the pinned
  coordinator `3639024497392091136`'s agent identity (and to throwaway validation
  engines, since torn down); redeployed the three MCP servers (stateless_http +
  `--min-instances 1`); in-place `deploy_agents coordinator --update` to recycle the
  pinned engine's containers (no `.env` change). All throwaway engines were torn down;
  the RE-service-agent grant (redundant given `editor`) was left in place.
- **Gateway/SGP is a separate layer.** The `roles/iap.egressor` + SGP CEL policies
  in `scripts/setup_governance_policies.sh` govern the Agent Gateway egress path —
  distinct from Agent Registry read and from the direct Cloud Run endpoint the
  toolset actually connects to.

Related: [offline-eval-monitoring-bridge](./offline-eval-monitoring-bridge.md),
[gemini3-native-model-resolution](./gemini3-native-model-resolution.md),
memory `online-eval-content-capture-blocked`.
