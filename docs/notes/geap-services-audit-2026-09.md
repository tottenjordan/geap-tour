# Agent Gateway, Agent Identity, Agent Registry — research + live audit

**2026-09-11.** Two halves: a cited literature review of the current GEAP surfaces
(104-agent deep-research run, 14 adversarially-verified claims), and a **live**
read-only audit of what `hybrid-vertex` actually has provisioned today. Where the two
disagree with our own docs, the live probe wins and is marked.

**No code was changed.** Everything below is read-only observation plus recommendation.

---

## Headline: our docs are wrong, and Agent Gateway is ready to turn on

`README.md` and `diagrams/inputs/04_agent_identity_gateway.txt` both say Agent Gateway
"is not enabled in this project" / "the `agentGateways` API 404s in `hybrid-vertex`
(private preview, early access not granted)". `deploy_agents.py:17` says the same
("requires early-access"), as does `setup_governance_policies.sh:250`.

**That is no longer true.** Measured 2026-09-11:

| check | result |
| --- | --- |
| `networkservices.googleapis.com/…/agentGateways` (us-central1) | **HTTP 200** |
| gateways present | **2** — `geap-workshop-gateway`, `geap-workshop-gateway-egress` |
| created / last updated | 2026-05-13 / 2026-05-16 |
| ours? | **yes** — `setup_agent_gateway.sh` defaults are exactly these two names |
| prerequisite APIs (`compute`, `networksecurity`, `networkservices`, `iap`, `dns`) | **all enabled** |
| engine eligibility (must be created after 2026-04-29) | **all three pass** (2026-08-12/13/14) |
| `spec.deploymentSpec.agentGatewayConfig` on any engine | **absent on all three** |

The earlier 404 in our docs is explainable: the resource lives on
**`networkservices.googleapis.com`**, not `aiplatform`. Probing the aiplatform host
returns an HTML 404 that looks like "not enrolled". I reproduced that mistake myself
before checking `setup_agent_gateway.sh` for the real host.

So the answer to *"should we turn it on?"* is **yes, but not by flipping the flag
alone** — see the deny-by-default trap below.

---

## What the research established (14 verified claims)

Sources are first-party `docs.cloud.google.com/gemini-enterprise-agent-platform/*`
unless noted. Confidence as returned by the verification pass.

### Agent Gateway

1. **Two modes, asymmetric runtime support** (high). Client-to-Agent (ingress,
   `governedAccessPath: CLIENT_TO_AGENT`) and Agent-to-Anywhere (egress,
   `AGENT_TO_ANYWHERE`). **Agent Runtime supports both; Gemini Enterprise supports
   egress only** — "Ingress traffic isn't supported" for GE. One Runtime instance may
   bind both simultaneously. Ingress requires agent and gateway in the same project;
   egress permits cross-project; both require the same region. **No gateway can bind
   to a reasoning engine created before 2026-04-29.**
   *Our two gateways are exactly one of each mode — correctly provisioned.*

2. **Deny-by-default via IAP** (high). "By default, all egress traffic is denied
   unless explicitly allowed by this IAM policy." Egress to any destination requires
   `iap.resources.egressViaIAP` granted to the **agent's SPIFFE identity** for that
   destination, evaluated per request. IAP is **not** supported on ingress, which uses
   authorization policies + Service Extensions instead. In enforcement mode the deny is
   aggressive enough to **block calls to internal Google services** (aiplatform,
   logging) unless registered and permitted. There is an audit-only escape:
   `iamEnforcementMode: DRY_RUN`.
   *Note: the permission string drifts across Google's own pages —
   `iap.resources.egressViaIAP`, `iap.googleapis.com/resources.egressViaIAP`,
   `iap.webServiceVersions.egressViaIAP` — all conveyed by `roles/iap.egressor`.*

3. **Declarative creation** (high). YAML + `gcloud network-services agent-gateways
   import NAME --source=file.yaml --location=LOCATION`.
   *Ours were created by an imperative REST script instead — see "simplifications".*

4. **Attachment to a reasoningEngine** (high). Via
   `spec.deploymentSpec.agentGatewayConfig` — `agent_gateway_config` at create time
   (with `agent_to_anywhere_config` and/or `client_to_agent_config`), or PATCH with
   `updateMask=spec.deploymentSpec.agentGatewayConfig`.
   *We implement create-time **egress only** (`deploy_agents.py:205`) and PATCH
   **ingress only** (`setup_governance_policies.sh:207`). Split across two places, and
   neither sets both.*

5. **Destinations register in Agent Registry** (medium). `gcloud agent-registry
   services create … --endpoint-spec-type=no-spec --interfaces=url=…,protocolBinding=jsonrpc`,
   plus `roles/iap.egressor` on the registry endpoint.
   *`deploy_all.sh:151` already does exactly this, with `protocolBinding=JSONRPC`.*

6. **Gemini Enterprise binds differently** (medium). Not a reasoningEngine — PATCH the
   discoveryengine `UpdateEngine` API with
   `updateMask=agentGatewaySetting.defaultEgressAgentGateway.name`, which **immediately
   reroutes all existing traffic**.

7. **Prerequisites** (high): `compute`, `networksecurity`, `networkservices`, `iap`,
   `dns` at minimum. *All enabled here.*

8. **VPC egress needs a second resource** (high): an `AgentConnectivityTemplate`
   carrying `egressNetworkConfig` (PSC networkAttachment, dnsPeeringConfig, vpcEgress
   mode), referenced by name from the gateway.
   *We have no `AgentConnectivityTemplate`. Our egress gateway is `googleManaged` with
   an mTLS PSC service attachment, so this may not apply — worth confirming before
   any VPC-scoped demo.*

9. **Shared VPC** (high): grants to `service-<PROJECT_NUMBER>@gcp-sa-agentgateway.iam.gserviceaccount.com`.

10. **Delegated authorization** (high): Service Extensions authorization extensions can
    hand the decision to IAP, **Model Armor**, Semantic Governance, or a custom engine,
    in exactly two profiles — `REQUEST_AUTHZ` / `CONTENT_AUTHZ` — capped at **four
    policies on egress, one CONTENT_AUTHZ on ingress**.
    *This is the most interesting unexploited capability we have: it would let Model
    Armor screen at the **network** chokepoint rather than only in-process.*

11. **Delegation prerequisites** (high): gateway already deployed; caller holds
    `networkservices.agentGateways.use`.

### Agent Identity

12. **SPIFFE ID shape** (medium): `spiffe://agents.global.org-<ORG_ID>.system.id.goog/resources/<service>/<resource-path>`,
    or a project-scoped variant without an org.
13. **First-class IAM principal** (high): granted directly as `principal://…` with **no
    intermediate service account**; agent identities **cannot be impersonated** and no
    long-lived credentials can be issued for them. Opt-in via
    `identity_type=AGENT_IDENTITY`; legacy service-account behaviour persists otherwise.
    *All three of our engines report `identityType=AGENT_IDENTITY` with a real
    `effectiveIdentity`. ID-2 is correctly done.*

### Agent Registry

14. **Registry is a hard dependency of the gateway data path** (medium): hierarchy is
    `iap_web/agentRegistry` parenting `agents` / `mcpServers` / `endpoints`.

**Honest gap in the research:** no verified claim addressed A2A agent cards,
`publishers`, `skills:search`, or the relationship between agentregistry skills and the
aiplatform `v1beta1` skills surface. Those questions are answered below from live
probing instead, not from documentation.

---

## Live audit — what we actually have

### Agent Identity: correct, and under-demonstrated

All three served engines: `identityType=AGENT_IDENTITY`, `effectiveIdentity` present.
`setup_governance_policies.sh:279 grant_registry_read` grants
`roles/agentregistry.viewer` to `principal://<effectiveIdentity>` per engine — the
narrow, correct pattern the docs recommend.

**What we don't demonstrate:** ID-3 (delegated / on-behalf-of user identity). The
README's identity table describes all three types, and we implement only ID-2. The GE
publication plan already notes `authorizationConfig.toolAuthorizations` as out of
scope. That is the single biggest Identity story we are not telling.

### Agent Registry: registered, but thin and accumulating

| surface (us-central1) | state |
| --- | --- |
| `mcpServers` | 10 — ours (`search-mcp`, `booking-mcp`, `expense-mcp`) **plus `wrangler-*` duplicates of all three** |
| `agents` | **34**, of which ~22 look like ours — but only **3 engines are live** |
| `endpoints` | **0** |
| `publishers` | 503 UNAVAILABLE (lives in `global`) |
| `skills` | 503 UNAVAILABLE (lives in `global`) |

Our engines are **auto-registered** by Agent Runtime with
`protocols: [{type: CUSTOM, interfaces: [:query, :streamQuery, HTTP_JSON]}]` and a URN
`agentId`. On all three: **`skills: null`**, `version: null`, protocol type `CUSTOM`
rather than `A2A`. Only **1 of 34** agents in the registry declares any skills.

Two concrete consequences:

* **Registry entries accumulate exactly like the skills did** (116 skills / 42 distinct
  names). `gepa-sonnet` appears twice; `_jt1`-tagged entries for all five tier agents
  persist though those engines are gone. We have `find_orphan_engines` for *engines* and
  **nothing** for registry agents or MCP servers.
* **The agent record's `skills` field may be the path that actually works.** The
  standalone aiplatform skills surface is invisible to ADK's `GCPSkillRegistry`
  (documented in `skill-registry.md`). Declaring skills **on the agent record** is a
  different, unexplored surface and is worth a spike before any more skills work.

### Provisioning sequence: one real ordering bug

`deploy_all.sh` runs: 6 Gateway → 7 register MCP in Registry → 8 deploy agents →
10 governance (IAM) → 11 verify.

**Step 10 grants the per-engine `roles/agentregistry.viewer`, but nothing recycles the
engines afterwards.** MCP toolsets resolve **once per container**, at step 8 — before
the grant exists. So every fresh end-to-end run ends with engines silently on the
direct-Cloud-Run-URL fallback path, signalled only by a WARNING in engine logs.

This is not hypothetical: it is the incident in
`agent-registry-mcp-resolution.md`, remediated by hand on 2026-08-15 and again on
08-19. The orchestration still reproduces it.

`setup_governance_policies.sh` **documents the requirement in a comment** at the grant
site — *"an existing engine caches its toolset resolution per container instance, so the
cutover completes when the engine recycles — e.g. an in-place `deploy_agents … --update`"*
— and `deploy_all.sh` never does it.

**It is not simply reorderable.** `grant_registry_read` reads `effectiveIdentity` **off
the deployed engine spec**, so the grant genuinely cannot precede deploy. The correct
sequence is deploy → grant → **recycle**.

### Other provisioning observations

* **The global gateway pair was never created.** `setup_agent_gateway.sh` defines
  `geap-workshop-ge-gateway` and `-ge-gateway-egress` for the Gemini Enterprise path;
  `locations/global` has **zero** gateways. Given finding 6 (GE binds a discoveryengine
  Engine, not a reasoningEngine) and that GE publication is parked on a license, this
  half of the script is dead weight today.
* **The egress gateway's `registries` pointer is correct**, not a bug:
  `//agentregistry.googleapis.com/projects/hybrid-vertex/locations/us-central1`. Skills
  503 at that location, but the gateway declares `protocols: [MCP]` and `mcpServers`
  returns 200 with our three servers.
* **Naming is inconsistent across the estate**: gateways use `geap-workshop-*`, resource
  labels use `solution=geap-tour`, engines use `_jt1` / bare / `-probe` suffixes. Fine
  individually; collectively it makes "is this ours?" a manual judgement — which is
  exactly the question I could not answer from labels when auditing the gateways.

---

## Recommendations

Ordered by value per unit of risk. **None of these were applied.**

### 1. Correct the docs first — they are actively misleading (trivial, zero risk)

Four places say Agent Gateway is unavailable/early-access when it is provisioned and
the API answers: `README.md`, `diagrams/inputs/04_agent_identity_gateway.txt`,
`deploy_agents.py:17`, `setup_governance_policies.sh:250`. Anyone reasoning about
Gateway from our repo today concludes, wrongly, that it is out of reach.

### 2. Fix the deploy → grant → **recycle** ordering (small, high value)

Add an in-place `deploy_agents … --update` after governance in `deploy_all.sh`, or move
the grant into the deploy path once `effectiveIdentity` is readable. Until then every
clean run silently degrades to the fallback path.

### 3. Turn Gateway on — egress first, probe engine only, DRY_RUN before enforce

The flag alone is not enough, and flipping it blind is the risk worth naming: **egress
is deny-by-default and can block the engine's own calls to aiplatform and logging.**
A safe order:

1. `iamEnforcementMode: DRY_RUN` on the gateway; confirm from audit logs what *would*
   be denied.
2. Register every destination (three MCP Cloud Run URLs) — `deploy_all.sh:151` already
   has the command shape.
3. Grant `roles/iap.egressor` on each destination to the engine's `principal://…`.
4. `ENABLE_AGENT_GATEWAY=1` and in-place `--update` on the **probe engine 4380…** only,
   diffing engine env before and after.
5. Verify, then roll to coordinator `3639…` and router `6134…`.

Also worth closing: `deploy_agents` sets only `agent_to_anywhere_config` while the
governance script PATCHes only `client_to_agent_config`. One deploy path should be able
to set both, since Agent Runtime supports both simultaneously.

### 4. The best unexploited demo: Model Armor at the gateway (finding 10)

We currently screen in-process (client guardrail + templates/plugin). Service Extensions
`CONTENT_AUTHZ` would let Model Armor screen at the **network chokepoint** — a
governance story we can show but currently cannot. It also directly addresses the
`eval-reliability-audit` finding that our only safety test is a tautological blocklist:
a network-enforced control is testable in a way a regex list is not.

### 5. Agent Identity: demonstrate ID-3

Delegated / on-behalf-of user identity via `authorizationConfig.toolAuthorizations` is
the one identity type we describe and never show. Pairs naturally with GE publication,
which is parked on a license.

### 6. Registry hygiene and richer registration

* Add an orphan scan for registry `agents` / `mcpServers`, mirroring
  `find_orphan_engines`. 34 agents against 3 live engines, with duplicates.
* Spike declaring `skills` **on the agent record** — a surface we have never tried, and
  possibly the one that actually reaches ADK.
* We register **zero** `endpoints`. Worth understanding whether that surface is the
  right home for the MCP Cloud Run URLs currently registered as `mcpServers`.

---

## Open questions I could not settle read-only

* Whether the `wrangler-*` MCP servers and several `gepa-*` / tier agents in the
  registry are ours (from a sibling repo) or another team's. Labels are absent on
  registry entries, so ownership is not derivable.
* Whether `AgentConnectivityTemplate` (finding 8) applies to our `googleManaged`
  gateways or only to customer-managed VPC egress.
* Whether the `roles/iap.egressor` grants in `setup_governance_policies.sh` have ever
  been applied — they bind at resource scope, and the project-level policy shows no IAP
  roles.
