# Running Layer 1 Governance Against the Live Project

## Context

PR #121 corrected `scripts/setup_governance_policies.sh` so Layer 1 applies real IAM
policies instead of writing three files to `/tmp` and printing `IAM policy created`.
The code has never been run against `hybrid-vertex`. All three MCP servers still
return `{"etag":"ACAB"}` — an empty policy, zero bindings, nothing ever applied.

This plan runs it. The goal is a governance layer that is **real and inspectable** —
`get-iam-policy` returning actual conditional bindings against each engine's SPIFFE
identity — while remaining **unenforced**, because IAP evaluates only at the Agent
Gateway boundary and no engine carries `agentGatewayConfig`.

**The reason this needs a plan rather than a command: a default run does much more
than Layer 1.** Research for this plan found Layer 3 is ungated and would, on the
shared project:

* create `geap-model-armor-extension` and `geap-model-armor-policy` (CONTENT_AUTHZ on
  the ingress gateway) — neither exists today;
* grant `roles/modelarmor.calloutUser` and `roles/serviceusage.serviceUsageConsumer`
  to `service-934903580331@gcp-sa-dep.iam.gserviceaccount.com` at **project level**;
* report `✓ created` for every one of those regardless of outcome. Verified: `curl -s
  -X POST` exits **0** on an HTTP 401, and the block is `curl … && ok "created" ||
  warn "may already exist"`. This is the same false-success class the Layer 1 work
  just removed, still live in Layer 3.

The script's own usage text already says *"Without `--sgp`, only IAM Allow policies
(Layer 1) are set up."* That is not true today. Phase 1 makes it true.

### Verified live state (read-only, this session)

| Fact | Value |
| --- | --- |
| MCP server policies | `{"etag":"ACAB"}` — empty, all three. First apply is clean; the PR #121 precheck will pass |
| Coordinator `3639…` / Router `6134…` | `identityType=AGENT_IDENTITY`, `effectiveIdentity` present, **`agentGatewayConfig` not set** |
| `.env` `ENABLE_AGENT_GATEWAY` | `false` → with PR #121, Step 0 skips and attaches nothing |
| `roles/iap.egressor` | exists (BETA) — the apply will not fail on an unknown role |
| `geap-iap-extension` / `geap-iap-policy` | already exist (May 2026) |
| `geap-model-armor-*` | do **not** exist |
| Model Armor templates | `geap-workshop-prompt` / `geap-workshop-response` exist |
| Operator | `admin@jordantotten.altostrat.com`, holds `roles/iam.securityAdmin` + `roles/editor` — sufficient, and a reminder of blast radius |

### Decisions taken

1. **Layer 1 only** for this run — gate Layer 3 behind `--layer3` first.
2. **Stop at applied, not enforced.** `ENABLE_AGENT_GATEWAY` stays `false`.
3. **Fix Layer 3's reporting** in the same PR as the gate.

---

## Phase 0: Prerequisite

Merge **PR #121** (`MERGEABLE/CLEAN`, Unit Tests SUCCESS). Without its Decision 1 gate,
any real run attaches the gateway to both served engines and enforcement goes live
unattended. Nothing below may run before this lands.

---

## Phase 1: Gate Layer 3 and make it report honestly

Branch `layer3-gate` off the merged `main`. One PR.

**File:** `scripts/setup_governance_policies.sh`

### 1a. Add the flag

Mirror the existing `--sgp` shape exactly (`:49-56`):

```bash
ENABLE_SGP=false
ENABLE_LAYER3=false
DRY_RUN=false
for arg in "$@"; do
    case "$arg" in
        --sgp) ENABLE_SGP=true ;;
        --layer3) ENABLE_LAYER3=true ;;
        --dry-run) DRY_RUN=true ;;
    esac
done
```

Update the usage header (`:31-35`) and the banner line beside the SGP one (`:176`).

### 1b. Gate the block

Wrap **lines 1109–1192** (`step "Layer 3: …"` through the closing `echo ""`) in
`if ! $ENABLE_LAYER3; then … else … fi`, following the Layer 2 skip at `:834` — the
skip branch should say what Layer 3 *would* do, as Layer 2's does. Gate the summary's
Layer 3 block (`:1256-1263`) the same way; it currently prints all four resource names
unconditionally, including the two that do not exist.

### 1c. Replace the false success

Add one helper beside `run_cmd`, and route 3a / 3d / 3e (and the Layer 2 SGP POSTs, if
they share the pattern) through it:

```bash
# curl -s exits 0 for ANY completed transfer, a 401/403/409 included, so the
# `curl … && ok "created" || warn "may already exist"` this block used reported
# "created" on every failure — the same false success Layer 1 was just cured of.
# -w appends the status on its own line; everything before it is the body.
post_resource() {   # label, url, json_body
    local label="$1" url="$2" body="$3"
    if $DRY_RUN; then
        echo "    [dry-run] POST ${url}"
        return 0
    fi
    local out code
    out="$(curl -s -w $'\n%{http_code}' -X POST "$url" \
        -H "Authorization: Bearer ${ACCESS_TOKEN}" \
        -H "Content-Type: application/json" -d "$body")"
    code="${out##*$'\n'}"
    case "$code" in
        2*)  ok "${label}: created"; L3_CREATED=$((L3_CREATED + 1)) ;;
        409) ok "${label}: already exists" ;;
        *)   fail "${label}: HTTP ${code}"
             printf '%s\n' "${out%$'\n'*}" | head -5
             L3_FAILURES=$((L3_FAILURES + 1)); return 1 ;;
    esac
}
```

`409` is a genuine success for an idempotent create and must stay distinct from both
`2xx` and a real error — collapsing it back into "may already exist" is how the current
bug reads to an operator.

### 1d. Guard it

Extend `tests/test_governance_policies.py` (created in PR #121):

* Layer 3 is gated — `ENABLE_LAYER3` is parsed before `authzExtensions` is POSTed, and
  the default is `false`.
* No `curl … && ok` survives in the Layer 3 span — assert the bare pattern is gone.
* `post_resource` distinguishes `409` from `2xx` from everything else.
* Reuse the mutation discipline from PR #121: revert each guard's target and confirm
  the suite goes red before committing.

**Gate:** `uv sync --all-groups && uv run --no-sync ruff format --check && uv run
--no-sync ruff check && uv run --no-sync ty check src/ && uv run --no-sync pytest`
(expect 1824 + new). `bash -n scripts/setup_governance_policies.sh`.

Open the PR. **Do not merge without approval.** Nothing below runs until it lands.

---

## Phase 2: Capture a rollback baseline

Read-only. Do this even though the answer is known to be empty — the point is a file to
restore from, not a fact to learn.

```bash
mkdir -p /tmp/geap-l1-baseline && cd /home/user/geap/geap-tour
set -a; source .env; set +a

for v in SEARCH BOOKING EXPENSE; do
  eval "s=\$${v}_MCP_SERVER"
  gcloud beta iap web get-iam-policy --resource-type=agent-registry \
    --mcp-server="$(basename "$s")" --region=us-central1 --project=hybrid-vertex \
    --format=json > "/tmp/geap-l1-baseline/${v}.json"
done

gcloud projects get-iam-policy hybrid-vertex --format=json \
  > /tmp/geap-l1-baseline/project-iam.json
gcloud beta service-extensions authz-extensions list --location=us-central1 \
  --project=hybrid-vertex > /tmp/geap-l1-baseline/authz-extensions.txt
gcloud beta network-security authz-policies list --location=us-central1 \
  --project=hybrid-vertex > /tmp/geap-l1-baseline/authz-policies.txt
```

Confirm each of the three shows `{"etag":"ACAB"}` and no `bindings`. **If any already
has bindings, stop** — the PR #121 precheck will abort the apply anyway, and that is a
finding (someone else is governing these servers) rather than an obstacle to route
around.

---

## Phase 3: Dry run

```bash
bash scripts/setup_governance_policies.sh --dry-run 2>&1 | tee /tmp/geap-l1-dryrun.log
```

Read the log and confirm, before anything mutates:

* Step 0 prints **SKIPPED — ENABLE_AGENT_GATEWAY is 'false'**, and no `attach_gateway`
  or `identityType` PATCH appears anywhere.
* Layer 3 prints its **SKIPPED** branch; no `authzExtensions` or `authzPolicies` POST
  appears, and no `add-iam-policy-binding` for the `gcp-sa-dep` SA.
* Exactly **three** `gcloud beta iap web set-iam-policy` lines, each with
  `--mcp-server=<bare id>` — never empty, never a full resource path.
* Layer 2 prints SKIPPED.
* The summary reports `[dry-run] nothing written, nothing applied`.

A dry run writes no policy files (`write_egress_policy` guards on `$DRY_RUN` before its
`rm`), so `/tmp/iam-policy-*.json` should be untouched.

---

## Phase 4: The live run

```bash
bash scripts/setup_governance_policies.sh 2>&1 | tee /tmp/geap-l1-apply.log
```

Expected, in order:

1. **Step 0** — SKIPPED. Nothing attached.
2. **Step 0b** — `roles/agentregistry.viewer` granted to both `principal://…`
   identities. These were already applied (2026-08-15 / 08-19) and
   `add-iam-policy-binding` is additive, so this is an idempotent no-op.
3. **Layer 1** — three files written, three etags read, three policies applied.
   Summary: `✓ 3/3 applied — no engine attached to a gateway, so not yet enforced`.
4. **Layer 2 / Layer 3** — SKIPPED.

**Stop immediately if** the precheck reports a foreign binding, or Layer 1 applies
fewer than 3, or Step 0 attaches anything. Any of those means reality differs from the
baseline above, and the right response is to read, not to re-run.

---

## Phase 5: Verify

```bash
cd /home/user/geap/geap-tour && set -a; source .env; set +a

# 1. Real bindings now, where etag ACAB used to be
for v in SEARCH BOOKING EXPENSE; do
  eval "s=\$${v}_MCP_SERVER"; echo "--- $v ---"
  gcloud beta iap web get-iam-policy --resource-type=agent-registry \
    --mcp-server="$(basename "$s")" --region=us-central1 --project=hybrid-vertex
done
```

Each must show `roles/iap.egressor`, **both** `principal://agents.global.org-…`
members, and its condition — read-only for search, non-destructive-plus-`cancel_booking`
for booking, the three-tool allowlist for expense.

```bash
# 2. Nothing is enforced: no engine carries agentGatewayConfig
uv run python -m src.deploy.verify_engine_config          # 0 critical; gateway_attached advisory

# 3. Tools still resolve — 10 tools, three servers
uv run python -m src.eval.verify_mcp_tools --json

# 4. The demo is unchanged, which is the real success criterion
uv run python -m src.eval.multi_agent_batch_eval --agents coordinator_agent --limit 8
```

**Success:** real conditional bindings on all three servers; `verify_engine_config`
still reports 0 critical with the gateway unattached; all 10 tools resolve; the
coordinator's rubric scores are unchanged, *because nothing is enforced*. A drop in (4)
would mean something is being enforced that should not be — treat it as a rollback
trigger, not as noise.

---

## Rollback

Cheap, because the prior state was empty. Per server:

```bash
ETAG=$(gcloud beta iap web get-iam-policy --resource-type=agent-registry \
  --mcp-server="<id>" --region=us-central1 --project=hybrid-vertex \
  --format="value(etag)")
printf '{"version":3,"bindings":[],"etag":"%s"}\n' "$ETAG" > /tmp/revert.json
gcloud beta iap web set-iam-policy /tmp/revert.json --resource-type=agent-registry \
  --mcp-server="<id>" --region=us-central1 --project=hybrid-vertex --quiet
```

Re-read the fresh etag rather than reusing `ACAB` from the baseline — it changes on
every write. Step 0b's grants need no rollback (idempotent, pre-existing, and the
documented fix for the router's 403 fallback). Nothing else was touched.

---

## Phase 6: Record the outcome

Extend the *Corrected Layer 1* section of
`docs/notes/geap-services-audit-2026-09.md` (added in PR #121), which currently ends
with **"the apply has not been run against the live project"**:

* the applied bindings, and the `get-iam-policy` output proving `ACAB` is gone;
* that the demo was unchanged, with the batch-eval numbers;
* the Layer 3 finding and its gate — including that `geap-iap-*` had existed since May
  2026 while the script reported creating them on every run;
* anything the dry run predicted wrongly. A divergence between the printed plan and the
  real run is the most valuable thing this exercise can produce.

`docs/notes/README.md` is at **199 of 200 lines** and the audit note is already indexed
at line 84 — extending it needs no new index line. Commit via PR.

---

## Caveats

* **`setup_governance_policies.sh` mutates IAM on a shared project.** Phase 4 needs an
  explicit go-ahead at the moment of running, not just approval of this plan.
* **Applied is not enforced, and the summary says so.** The single most likely
  misreading of a green run is that governance is now live. It is not until an engine
  carries `agentGatewayConfig`.
* **Do not set `ENABLE_AGENT_GATEWAY=1` as part of this.** Egress is deny-by-default via
  IAP and can block an engine's own aiplatform and logging calls. That is a separate
  exercise, probe engine `4380…` first, per recommendation 3 of the audit note.
* **Never repoint `AGENT_ENGINE_ID`**, and never recreate an engine — a new engine mints
  a new identity and silently invalidates every binding applied here.
* There is no `iamEnforcementMode: DRY_RUN` to fall back on: `gcloud iap settings`
  exposes no enforcement flag and `agent-registry` is not a valid `--resource-type` for
  it. `--dry-run` in Phase 3 is the script's own flag, not a server-side audit mode.
