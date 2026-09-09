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

## The Gemini-3 armor gap, and the first-party plugin (2026-09-08)

`get_armored_generate_config` attaches Model Armor templates only for a regional
Gemini-2.x backbone. That gate is correct — Gemini-3 runs on the global endpoint
(templates `400 TEMPLATE_NOT_FOUND`) and Claude runs via LiteLlm — but it means
**server-side screening silently disappears when the backbone moves**, leaving the
client-side blocklist as the only layer.

**Measured, and narrower than it first looked.** The live coordinator `3639…` is
baked with `COORDINATOR_MODEL=gemini-2.5-flash`, so templates *are* active on it
today. The gap is **latent, not an active outage**: `.env` sets
`AGENT_MODEL=gemini-3.5-flash`, so the next coordinator deploy would drop to one
layer — and the bake-off engines already run Gemini-3 backbones. (An earlier draft
of this note claimed production was unarmored. It was not; the check below is what
established that.)

**ADK 2.8.0 supplies the remedy that did not previously exist.**
`google.adk.integrations.model_armor.ModelArmorPlugin` screens inside the ADK
request path rather than through a `GenerateContentConfig` field, so it is
model-family-independent. `src/armor/config.py:model_armor_plugin` wires it behind
`ENABLE_MODEL_ARMOR_PLUGIN` (default OFF), reusing the same two templates the repo
already provisions, and returns `None` on a regional Gemini-2.x backbone so the two
layers never double-screen the same request.

Three things worth knowing before enabling it:

* It needs **`google-cloud-modelarmor`**, which is now in `deploy_agents.REQUIREMENTS`.
  ADK suggests `google-adk[gcp]` for it; that extra caps `google-cloud-aiplatform<2`
  and would fight our 2.x pin, so depend on the package directly.
* The flag is **baked into the engine env** on deploy, so a deployed spec records
  which layers it actually serves with. Without that, a plugin-armored Gemini-3
  engine is indistinguishable from an unarmored one.
* `src/armor/config.py:armor_layers` is the single "are we covered?" answer, and
  `engine_baseline`'s `server_side_armor` check now accepts **either** layer.

**It stays ADVISORY, deliberately.** Making it critical before the flag is rolled
out would red every Gemini-3 engine for a gap that has no deployed fix yet.
Escalating to critical is the natural follow-up once `ENABLE_MODEL_ARMOR_PLUGIN=1`
is live on the served engines.
