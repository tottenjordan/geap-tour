#!/usr/bin/env bash
# Setup Model Armor templates for Agent Armor — input and output screening

# Loads .env and provides PROJECT_ID / REGION / project_number / require_var.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/config.sh"
set -euo pipefail

PROMPT_TEMPLATE_NAME="geap-workshop-prompt"
RESPONSE_TEMPLATE_NAME="geap-workshop-response"

# Counts real outcomes, so the closing "setup complete" is a claim this script has
# earned rather than a line that prints no matter what happened. See http_send in
# lib/config.sh for why `curl … && echo created` could not tell a 200 from a 403.
MA_FAILURES=0

create_template() {   # label, template-id, body
    local label="$1" template_id="$2" body="$3"
    local url="https://modelarmor.${REGION}.rep.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/templates?template_id=${template_id}"

    if http_send POST "${url}" "${body}"; then
        case "${HTTP_STATUS}" in
            409) echo "  ✓ ${label}: already exists (unchanged)" ;;
            *)   echo "  ✓ ${label}: created" ;;
        esac
        return 0
    fi

    # Named loudly because this is the server-side half of the armor story: the
    # templates these create are what get_armored_generate_config attaches, and what
    # the console Security tab reads. A silent miss here is armor that is simply not
    # there, on a run that said it was.
    echo "  ✗ ${label}: HTTP ${HTTP_STATUS} — NOT created" >&2
    printf '%s\n' "${HTTP_BODY}" | head -5 >&2
    MA_FAILURES=$((MA_FAILURES + 1))
    return 1
}

echo "=== Setting up Model Armor Templates ==="
echo "Project: $PROJECT_ID"
echo "Region: $REGION"

# Enable Model Armor API
echo "[1/4] Enabling Model Armor API..."
gcloud services enable modelarmor.googleapis.com --project="$PROJECT_ID"

# Create prompt screening template (input guardrails)
echo "[2/4] Creating prompt screening template..."
PROMPT_FILTER='{
    "labels": {"solution": "geap-tour"},
    "templateMetadata": {
        "enforcementType": "INSPECT_ONLY",
        "logTemplateOperations": true,
        "logSanitizeOperations": true
    },
    "filterConfig": {
        "raiSettings": {
            "raiFilters": [
                {"filterType": "DANGEROUS", "confidenceLevel": "MEDIUM_AND_ABOVE"},
                {"filterType": "HARASSMENT", "confidenceLevel": "MEDIUM_AND_ABOVE"},
                {"filterType": "HATE_SPEECH", "confidenceLevel": "MEDIUM_AND_ABOVE"},
                {"filterType": "SEXUALLY_EXPLICIT", "confidenceLevel": "HIGH"}
            ]
        },
        "piAndJailbreakFilterSettings": {
            "filterEnforcement": "ENABLED",
            "confidenceLevel": "MEDIUM_AND_ABOVE"
        },
        "maliciousUriFilterSettings": {
            "filterEnforcement": "ENABLED"
        }
    }
}'

create_template "Prompt template" "${PROMPT_TEMPLATE_NAME}" "$PROMPT_FILTER" || true

# Create response screening template (output guardrails)
echo "[3/4] Creating response screening template..."
RESPONSE_FILTER='{
    "labels": {"solution": "geap-tour"},
    "templateMetadata": {
        "enforcementType": "INSPECT_ONLY",
        "logTemplateOperations": true,
        "logSanitizeOperations": true
    },
    "filterConfig": {
        "raiSettings": {
            "raiFilters": [
                {"filterType": "DANGEROUS", "confidenceLevel": "MEDIUM_AND_ABOVE"},
                {"filterType": "HARASSMENT", "confidenceLevel": "MEDIUM_AND_ABOVE"},
                {"filterType": "HATE_SPEECH", "confidenceLevel": "MEDIUM_AND_ABOVE"},
                {"filterType": "SEXUALLY_EXPLICIT", "confidenceLevel": "HIGH"}
            ]
        },
        "maliciousUriFilterSettings": {
            "filterEnforcement": "ENABLED"
        }
    }
}'

create_template "Response template" "${RESPONSE_TEMPLATE_NAME}" "$RESPONSE_FILTER" || true

# Grant the agent service account Model Armor roles
echo "[4/4] Granting IAM roles..."
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
SA_EMAIL="service-${PROJECT_NUMBER}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
# `2>/dev/null || true` discarded both the error and the fact there had been one.
# add-iam-policy-binding is idempotent and exits 0 when the binding already exists,
# so a non-zero status here is always a real failure — and these two roles are what
# let the Reasoning Engine service agent call Model Armor at all. Without them the
# templates above exist and are never consulted.
for role in roles/modelarmor.user roles/modelarmor.calloutUser; do
    if gcloud projects add-iam-policy-binding "$PROJECT_ID" \
        --member="serviceAccount:${SA_EMAIL}" \
        --role="$role" \
        --condition=None \
        --quiet >/dev/null; then
        echo "  ✓ ${role} granted to ${SA_EMAIL}"
    else
        echo "  ✗ ${role} grant FAILED (gcloud's error is above this line)" >&2
        MA_FAILURES=$((MA_FAILURES + 1))
    fi
done

# The grants above are for the SERVICE AGENTS, which serve the Gemini-2.x template path
# where Vertex calls Model Armor on the engine's behalf. They are not enough.
#
# On a Gemini-3 backbone the templates are omitted (regional-only) and ADK's
# ModelArmorPlugin screens inside the engine instead — calling Model Armor as the
# ENGINE'S OWN AGENT_IDENTITY. That principal held no modelarmor role at all, the call
# failed, and block_on_screening_failure=True turned it into a 100% refusal rate on a
# fresh coordinator. See docs/notes/ and lib/config.sh:grant_modelarmor_user.
echo "  Granting roles/modelarmor.user to the ENGINE identities..."
for pair in "Coordinator:${AGENT_ENGINE_ID:-}" "Router:${ROUTER_ENGINE_ID:-}"; do
    grant_modelarmor_user "${pair%%:*}" "${pair#*:}" || MA_FAILURES=$((MA_FAILURES + 1))
done

PROMPT_TEMPLATE="projects/${PROJECT_ID}/locations/${REGION}/templates/${PROMPT_TEMPLATE_NAME}"
RESPONSE_TEMPLATE="projects/${PROJECT_ID}/locations/${REGION}/templates/${RESPONSE_TEMPLATE_NAME}"

echo ""
# Was unconditional, directly beneath four steps that could each fail without saying
# so. "Complete" is now something the run has to have earned, and a failed run exits
# non-zero so a caller (deploy_all.sh, CI, a person in a terminal) can tell.
if [ "${MA_FAILURES}" -eq 0 ]; then
    echo "✓ Model Armor setup complete"
else
    echo "✗ Model Armor setup INCOMPLETE — ${MA_FAILURES} step(s) failed; see above." >&2
    echo "  The names below are what was ATTEMPTED, not what exists." >&2
fi
echo ""
echo "Templates:"
echo "  Prompt:   $PROMPT_TEMPLATE"
echo "  Response: $RESPONSE_TEMPLATE"
echo ""
echo "Add to .env:"
echo "  MODEL_ARMOR_PROMPT_TEMPLATE=$PROMPT_TEMPLATE"
echo "  MODEL_ARMOR_RESPONSE_TEMPLATE=$RESPONSE_TEMPLATE"

[ "${MA_FAILURES}" -eq 0 ]
