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

# .env wins over nothing, but NOT over an already-exported variable: an explicit
# `GCP_PROJECT_ID=other bash scripts/foo.sh` must still override the file.
if [ -f "${REPO_ROOT}/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "${REPO_ROOT}/.env"
    set +a
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
