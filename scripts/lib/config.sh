# Shared configuration for every script in scripts/. Source it first:
#
#     source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/config.sh"
#
# WHY THIS EXISTS
#
# `.env` is this repo's single source of truth for deployed identifiers — src/config.py
# reads it, deploys write back to it, verify_engine_config checks against it. But only
# 3 of 14 shell scripts ever sourced it. The other 11 re-derived the same values from
# their own `${GCP_PROJECT_ID:-hybrid-vertex}` defaults, so a project configured in
# .env was ignored by most of the tooling that acts on it.
#
# The duplication also drifts in the way that costs: setup_apphub.sh carried
# `${PROJECT_NUMBER:-934903580331}` plus two engine-id fallbacks that both pointed at
# DELETED engines, so an unset variable did not fail — it registered non-existent
# engines in App Hub. Ten copies of a default are ten chances for one to go stale
# unnoticed.
#
# So: one place that loads .env, one place that names a default.

set -euo pipefail

_GEAP_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${_GEAP_LIB_DIR}/../.." && pwd)"

# .env fills in what the environment does not already set — an explicit
# `GCP_PROJECT_ID=other bash scripts/foo.sh` must still win over the file.
#
# This is NOT what `set -a; source .env` does. Sourcing runs every assignment
# unconditionally, so the file silently clobbered the caller's variable: someone
# running `GCP_PROJECT_ID=my-sandbox bash scripts/setup_governance_policies.sh`
# believed they were targeting their own project and were in fact granting IAM in
# hybrid-vertex. The comment here claimed the correct behaviour while the code did
# the opposite, and nothing tested it.
#
# It also has to agree with the Python half of the repo: `src/config.py` calls
# `load_dotenv()`, which defaults to `override=False`. Before this, the same
# variable resolved one way through Python and the other way through bash.
#
# Only KEY=VALUE lines are evaluated, which is also strictly safer than sourcing —
# an arbitrary command in .env is ignored rather than executed. `eval` on the
# matched line preserves the quoting semantics sourcing gave us.
if [ -f "${REPO_ROOT}/.env" ]; then
    while IFS= read -r _geap_line || [ -n "$_geap_line" ]; do
        [[ $_geap_line =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)= ]] || continue
        _geap_key="${BASH_REMATCH[2]}"
        # Set already — including deliberately set to empty — so the caller wins.
        [ -n "${!_geap_key+x}" ] && continue
        eval "export ${_geap_line}"
    done < "${REPO_ROOT}/.env"
    unset _geap_line _geap_key
fi

# The single home for this default, mirroring src/config.py's. A literal here is
# fine; ten copies of it are not.
PROJECT_ID="${GCP_PROJECT_ID:-hybrid-vertex}"
REGION="${GCP_REGION:-us-central1}"
STAGING_BUCKET="${GCP_STAGING_BUCKET:-${PROJECT_ID}-geap-staging}"
export PROJECT_ID REGION STAGING_BUCKET

# Derived, never hardcoded. The literal 934903580331 only works in one project, and
# a wrong project number fails deep inside an App Hub call rather than here. Looked
# up lazily so sourcing this file never costs a network round-trip.
project_number() {
    if [ -n "${PROJECT_NUMBER:-}" ]; then
        printf '%s' "${PROJECT_NUMBER}"
        return 0
    fi
    local resolved
    resolved="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)' 2>/dev/null)"
    if [ -z "${resolved}" ]; then
        echo "ERROR: could not resolve the project number for '${PROJECT_ID}'." >&2
        echo "       Set PROJECT_NUMBER in .env, or grant projects.get on it." >&2
        return 1
    fi
    printf '%s' "${resolved}"
}

# Fail loudly rather than guessing. Used for deployed engine ids, which have no safe
# default: a stale literal keeps working right up until the engine is deleted, and
# then does something wrong instead of nothing.
require_var() {
    local name="$1"
    if [ -z "${!name:-}" ]; then
        echo "ERROR: ${name} is unset. Source .env or export it — scripts here do not" >&2
        echo "       fall back to hardcoded resource ids (they go stale silently)." >&2
        return 1
    fi
    printf '%s' "${!name}"
}

# ─────────────────────────────────────────────────────────────
# Honest REST creates
# ─────────────────────────────────────────────────────────────
#
# `curl -s` exits 0 for any COMPLETED transfer. A 400, a 401, a 403 and a 409 are all
# "success" as far as `$?` is concerned — only a transport failure (DNS, refused
# connection) is non-zero. So the idiom that grew in several scripts here,
#
#     curl -s -X POST "$url" -d "$body" && echo "✓ created" || echo "may already exist"
#
# takes the `&&` branch on every outcome. It is not a reporting nit: in
# setup_governance_policies.sh it printed "created" for two resources that had existed
# since May 2026 and two more that did not exist at all, on every run, for months.
#
# Measured, not reasoned — a POST to a real endpoint with a junk bearer token:
#     $ curl -s -X POST ".../authzExtensions?..." -H "Authorization: Bearer nope" -d '{}'
#     $ echo $?
#     0
#
# `http_send` reads the status instead. It sets HTTP_STATUS and HTTP_BODY and returns
# non-zero on anything outside 2xx/409, leaving the caller to decide how loudly to say
# so. 409 is reported through HTTP_STATUS rather than folded into 2xx: an idempotent
# create finding its resource already present is a success, but it is a DIFFERENT fact
# from having created one, and conflating the two is exactly what let a 401 hide behind
# "may already exist".
#
# Correct usage was already in the repo — setup_agent_gateway.sh has always parsed
# `-w "%{http_code}"`. This just puts it where every script can reach it.
HTTP_STATUS=""
HTTP_BODY=""

http_send() {   # method, url, body, [bearer-token]
    local method="$1"
    local url="$2"
    local body="$3"
    local token="${4:-$(gcloud auth print-access-token 2>/dev/null)}"

    local out
    # -w puts the status on its own trailing line, so the body is everything before it.
    if ! out="$(curl -s -w $'\n%{http_code}' -X "${method}" "${url}" \
        -H "Authorization: Bearer ${token}" \
        -H "Content-Type: application/json" \
        -d "${body}")"; then
        HTTP_STATUS="000"
        HTTP_BODY="curl could not reach ${url%%\?*}"
        return 1
    fi

    HTTP_STATUS="${out##*$'\n'}"
    HTTP_BODY="${out%$'\n'*}"

    case "${HTTP_STATUS}" in
        2*|409) return 0 ;;
        *)      return 1 ;;
    esac
}

# ─────────────────────────────────────────────────────────────
# Agent identities and the grants that must name them
# ─────────────────────────────────────────────────────────────
#
# THE ONE PLACE that turns an engine into a principal. Prints `spec.effectiveIdentity`
# and returns 0; prints nothing and returns 1 when the field is absent (identityType is
# not AGENT_IDENTITY, or the GET failed).
#
# It lives here, not in one script, because the wrong-principal bug is what duplication
# of this costs. It has already been paid twice: the registry grant went to the Reasoning
# Engine SERVICE AGENT (fixed 2026-08-15), and setup_model_armor.sh granted
# roles/modelarmor.user to that same service agent while the engine calls Model Armor as
# its OWN SPIFFE identity — which fails closed and made a Gemini-3 coordinator refuse
# 100% of requests (diagnosed 2026-09-17).
#
# ACCESS_TOKEN is honoured when a caller already resolved one; otherwise this fetches its
# own, so a script that does not manage tokens can still use it.
engine_identity() {
    local engine_id="$1"
    local token="${ACCESS_TOKEN:-$(gcloud auth print-access-token 2>/dev/null)}"
    local api_base="https://${REGION}-aiplatform.googleapis.com/v1"
    local engine_path="projects/${PROJECT_ID}/locations/${REGION}/reasoningEngines/${engine_id}"

    local eff
    # `|| true` so an unreachable API or a missing token is an empty identity the caller
    # can report, not a pipefail that kills the whole script from a command substitution.
    eff=$(curl -s -H "Authorization: Bearer ${token}" "${api_base}/${engine_path}" \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('spec',{}).get('effectiveIdentity',''))" 2>/dev/null) || true
    [ -n "$eff" ] || return 1
    printf '%s' "$eff"
}

# Grant roles/modelarmor.user to an ENGINE'S OWN identity.
#
# ADK 2.8.0's ModelArmorPlugin screens inside the engine's request path, so the caller is
# the engine's AGENT_IDENTITY — not the Reasoning Engine service agent, and not the
# deployer. On a Gemini-3 backbone the plugin is the ONLY server-side layer (templates
# are regional and omitted there), and it defaults to block_on_screening_failure=True.
# So a missing grant is not reduced screening: it is every request answered with ADK's
# "I'm sorry, but I can't help with that request." Hence the loud failure below.
#
# Only modelarmor.user. calloutUser is for the Service-Extensions callout path at the
# gateway, not for an in-process plugin, and the service-agent grants stay as they are —
# they serve the Gemini-2.x template path.
#
# Returns 0 for the already-reported "no identity" skip, so a bare call under `set -e`
# does not kill the script; returns 1 only for a real grant failure.
grant_modelarmor_user() {
    local label="$1"
    local engine_id="$2"

    if [ -z "${engine_id}" ]; then
        echo "  ⚠ ${label}: no engine id — skipping Model Armor grant." >&2
        return 0
    fi

    local eff
    eff="$(engine_identity "${engine_id}")" || {
        echo "  ⚠ ${label}: no effectiveIdentity for ${engine_id} — skipping Model Armor grant." >&2
        echo "    (expected on a fresh install before the engines exist; deploy_all.sh" >&2
        echo "     grants it at step 10, once identities are resolvable.)" >&2
        return 0
    }

    # Guarded BEFORE the success line, because `ok …granted` is a CLAIM. Both of this
    # repo's other grant helpers printed it on a dry run before being corrected.
    if [ "${DRY_RUN:-false}" = "true" ]; then
        echo "    [dry-run] gcloud projects add-iam-policy-binding ${PROJECT_ID} --member=principal://${eff} --role=roles/modelarmor.user"
        return 0
    fi

    if gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member="principal://${eff}" \
        --role="roles/modelarmor.user" \
        --condition=None \
        --quiet >/dev/null; then
        echo "  ✓ ${label}: roles/modelarmor.user granted to the agent identity"
        return 0
    fi
    echo "  ✗ ${label}: roles/modelarmor.user grant FAILED (gcloud's error is above)." >&2
    echo "    A Gemini-3 engine without it refuses EVERY request (plugin fails closed)." >&2
    return 1
}
