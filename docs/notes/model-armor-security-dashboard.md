# Model Armor Security dashboard (console Security tab)

**What / why.** The GEAP deployment console's **Security tab** shows a Model Armor
dashboard with a banner: *"This Model Armor dashboard will only be fully populated
if Agent Gateway and Google Cloud MCP Servers are enabled with Model Armor and
agent tracing."* This note records what actually drives that dashboard, the path we
chose to populate it, and two honesty caveats to keep in front of a customer.

## What feeds the dashboard

Two independent feed paths (either is sufficient):

1. **Project-level Model Armor floor settings + Cloud Logging** — the **no-preview**
   path. A per-project global floor setting (`projects/<id>/locations/global/floorSetting`)
   enables project-wide Vertex AI sanitization; with **Cloud Logging of sanitization
   results** turned on, prompt/response/detector verdicts flow to Cloud Logging and
   surface on the dashboard (and raise Security Command Center findings on violations).
   Docs are explicit: *"You must enable Cloud Logging to view the sanitization results."*
2. **Agent-Gateway-governed agents with Model Armor** (CONTENT_AUTHZ) — **Private
   Preview**. Already scaffolded in this repo (`setup_agent_gateway.sh`,
   `setup_governance_policies.sh` Layer 3) but disabled (`ENABLE_AGENT_GATEWAY=false`).
   Out of scope; it's the only path that can Model-Armor our *custom* MCP tools.

Agent tracing (the banner's third clause) is already on — every engine is deployed
with `GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true` (`deploy_agents._build_config`).

## The path we chose: floor settings (inspect-only) + Cloud Logging

Chosen because it needs **no preview enrollment** and demonstrates governance
visibility without blocking anything live.

- **`scripts/setup_model_armor_floor_settings.sh`** — updates the global floor
  setting: enforcement on, **Vertex AI `INSPECT_ONLY`** (nothing blocked),
  **Cloud Logging on**, RAI + PI/jailbreak + malicious-URI filters mirroring the
  templates. Idempotent (the floor setting is a project singleton). Wired into
  `scripts/deploy_all.sh` step 4; checked (fail-soft) by `scripts/verify_deployment.sh`.
- **`scripts/setup_model_armor.sh`** — the two per-request templates
  (`geap-workshop-prompt` / `geap-workshop-response`) now carry
  `templateMetadata { enforcementType: INSPECT_ONLY, logTemplateOperations: true,
  logSanitizeOperations: true }`, so the coordinator's explicit armored
  `generate_content` path also logs to the same surface.

Enforcement stays **inspect-only** by design (`--vertex-ai-enforcement-type=INSPECT_ONLY`,
`templateMetadata.enforcementType=INSPECT_ONLY`); flip to `INSPECT_AND_BLOCK` to
actually gate.

### Provisioning commands
```bash
bash scripts/setup_model_armor.sh                 # templates now log + INSPECT_ONLY
bash scripts/setup_model_armor_floor_settings.sh  # global floor setting, inspect-only, logging on
bash scripts/verify_deployment.sh                 # confirms templates + floor setting (fail-soft on no read)
# Drive traffic (reuses INJECTED_QUERIES adversarial prompts):
uv run python -m src.traffic.generate_traffic <COORDINATOR_ENGINE_ID> --load
```
Then read it back: Logs Explorer (logName containing `modelarmor`), Security Command
Center findings, and the console Security-tab dashboard (needs `monitoring.viewer` +
SCC access on the viewer).

## Two honesty caveats (do not hide from the customer)

1. **Custom MCP servers ≠ "Google Cloud MCP Servers."** search/booking/expense are
   custom Cloud Run FastMCP servers registered in Agent Registry. The
   `GOOGLE_MCP_SERVER` floor setting only governs *Google-managed* MCP servers
   (e.g. `bigquery.googleapis.com/mcp`), so it produces **no data** for our tools —
   it's left as a commented opt-in in the script. Our custom tools can only be
   Model-Armored via the gateway CONTENT_AUTHZ path (preview, out of scope).
2. **LiteLlm/Claude coverage gap.** Floor-setting `VERTEX_AI` sanitization and the
   templates apply to the Gemini `generateContent` path. Our coordinator runs
   LiteLlm (gemini-3.x / Claude on the global endpoint), so dashboard *sanitization
   volume* is richest when the coordinator runs a **native Gemini** backbone during
   the demo — Claude turns won't register as Gemini sanitizations. Same platform
   shape as [[online-eval-content-capture-blocked]].
