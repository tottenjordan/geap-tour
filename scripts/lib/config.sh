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
