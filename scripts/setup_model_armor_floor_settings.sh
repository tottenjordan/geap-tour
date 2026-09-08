#!/usr/bin/env bash
# =============================================================================
# Model Armor — project-level FLOOR SETTINGS (inspect-only + Cloud Logging)
# =============================================================================
# Configures the project's global Model Armor floor setting so that Vertex AI
# (generateContent) traffic is sanitized project-wide, in INSPECT_ONLY mode
# (nothing is blocked), with Cloud Logging of sanitization results turned on.
#
# WHY: the GEAP console Security tab's Model Armor dashboard only populates for
# agents in a project that has Model Armor floor settings configured OR that are
# governed by an Agent Gateway, AND with Cloud Logging of sanitization results
# enabled. Floor settings need no preview enrollment; the gateway CONTENT_AUTHZ
# path is Private Preview. See docs/notes/model-armor-security-dashboard.md.
#
# This is complementary to scripts/setup_model_armor.sh, which creates the two
# per-request templates (geap-workshop-prompt / geap-workshop-response) that the
# coordinator wires explicitly. The floor setting is a project singleton that
# catches the whole Vertex AI surface; the templates catch the coordinator's own
# armored generate_content path. Both feed the same Cloud Logging → dashboard.
#
# Idempotent: the floor setting is a per-project singleton that always exists,
# so this UPDATEs it (safe to re-run).
#
# Usage:
#   bash scripts/setup_model_armor_floor_settings.sh
#   GCP_PROJECT_ID=my-project bash scripts/setup_model_armor_floor_settings.sh
# =============================================================================
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-hybrid-vertex}"
# Floor settings are a GLOBAL, per-project singleton (not regional like templates).
FLOOR_URI="projects/${PROJECT_ID}/locations/global/floorSetting"

echo "=== Configuring Model Armor Floor Setting ==="
echo "Project: $PROJECT_ID"
echo "Floor:   $FLOOR_URI"
echo "Mode:    Vertex AI INSPECT_ONLY (nothing blocked) + Cloud Logging on"

# Enable Model Armor API (idempotent)
echo "[1/2] Enabling Model Armor API..."
gcloud services enable modelarmor.googleapis.com --project="$PROJECT_ID" --quiet

# Update the global floor setting. The RAI/PI/malicious-URI filters mirror the
# per-request templates in setup_model_armor.sh so both surfaces agree.
#
# Enforcement posture is INSPECT_ONLY: Model Armor evaluates and LOGS every
# prompt/response but never blocks — a governance-visibility demo, not a live
# gate. Flip --vertex-ai-enforcement-type=INSPECT_AND_BLOCK to actually enforce.
echo "[2/2] Updating global floor setting (inspect-only, logging on)..."

# --add-integrated-services uses the REST enum AI_PLATFORM (Vertex AI). If a
# future gcloud renames the enum, the Vertex-AI-specific flags below still
# configure the AiPlatformFloorSetting sub-message, so we retry without it.
FLOOR_FLAGS=(
    --full-uri="$FLOOR_URI"
    --enable-floor-setting-enforcement=TRUE
    --enable-vertex-ai-cloud-logging
    --vertex-ai-enforcement-type=INSPECT_ONLY
    --add-rai-settings-filters=filterType=DANGEROUS,confidenceLevel=MEDIUM_AND_ABOVE
    --add-rai-settings-filters=filterType=HARASSMENT,confidenceLevel=MEDIUM_AND_ABOVE
    --add-rai-settings-filters=filterType=HATE_SPEECH,confidenceLevel=MEDIUM_AND_ABOVE
    --add-rai-settings-filters=filterType=SEXUALLY_EXPLICIT,confidenceLevel=HIGH
    --pi-and-jailbreak-filter-settings-enforcement=ENABLED
    --pi-and-jailbreak-filter-settings-confidence-level=medium-and-above
    --malicious-uri-filter-settings-enforcement=ENABLED
)

if gcloud model-armor floorsettings update \
        "${FLOOR_FLAGS[@]}" \
        --add-integrated-services=AI_PLATFORM \
        --project="$PROJECT_ID" --quiet; then
    echo "  ✓ Floor setting updated (Vertex AI integrated)"
elif gcloud model-armor floorsettings update \
        "${FLOOR_FLAGS[@]}" \
        --project="$PROJECT_ID" --quiet; then
    echo "  ✓ Floor setting updated (Vertex AI flags only; integrated-services enum skipped)"
else
    echo "  ✗ Floor setting update failed — check IAM (roles/modelarmor.floorSettingsAdmin) and the enum values above" >&2
    exit 1
fi

# ── OPT-IN: Google-managed MCP server sanitization (DISABLED by default) ──────
# HONESTY CAVEAT: this governs only GOOGLE-managed MCP servers (e.g.
# bigquery.googleapis.com/mcp). Our search/booking/expense servers are CUSTOM
# Cloud Run FastMCP servers registered in Agent Registry — the Google MCP floor
# setting is a NO-OP for them, so it produces no dashboard data for our agents.
# Our custom MCP tools can only be Model-Armored via the Agent Gateway
# CONTENT_AUTHZ path (Private Preview, out of scope). Left commented on purpose:
#
#   gcloud model-armor floorsettings update \
#       --full-uri="$FLOOR_URI" \
#       --enable-google-mcp-server-cloud-logging \
#       --google-mcp-server-enforcement-type=INSPECT_ONLY \
#       --add-google-mcp-server-apis=bigquery.googleapis.com/mcp \
#       --project="$PROJECT_ID" --quiet

echo ""
echo "✓ Model Armor floor setting configured"
echo ""
echo "View results (after driving traffic through a Gemini-backed agent):"
echo "  • Security tab:  https://console.cloud.google.com/security/modelarmor?project=${PROJECT_ID}"
echo "  • Logs Explorer: filter for logName containing \"modelarmor\" (sanitization results)"
echo ""
echo "NOTE: floor-setting VERTEX_AI sanitization applies to the Gemini"
echo "generateContent path. The coordinator runs LiteLlm (gemini-3.x/Claude,"
echo "global endpoint); for the richest dashboard, run it on a NATIVE Gemini"
echo "backbone during the demo — Claude turns won't register as Gemini"
echo "sanitizations. See docs/notes/model-armor-security-dashboard.md."
