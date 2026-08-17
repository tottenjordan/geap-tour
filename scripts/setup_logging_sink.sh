#!/usr/bin/env bash
# Setup Cloud Logging + Monitoring access for the Agent Runtime: a BigQuery sink
# for agent traces plus the Viewer grants that let operators actually read the
# runtime's stdout/stderr logs AND the managed engine metrics. The managed
# runtime auto-routes stdout/stderr to the reasoning_engine_stdout /
# reasoning_engine_stderr log IDs and auto-emits request_count / request_latencies
# / cpu+memory allocation metrics on the aiplatform.googleapis.com/ReasoningEngine
# resource (no in-agent setup needed); viewing the logs in Logs Explorer / the
# Agent Runtime dashboard requires roles/logging.viewer, and viewing the metrics
# + auto "Agent Runtime Overview" dashboard requires roles/monitoring.viewer. See
# https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/logging
# https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/monitoring
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-hybrid-vertex}"
DATASET_NAME="${BQ_DATASET:-geap_workshop_logs}"
SINK_NAME="${SINK_NAME:-geap-agent-traces}"
# Principal to grant the observability Viewer roles (logging + monitoring).
# Defaults to the active gcloud account (as a user:); override to grant a
# group/service account, e.g. LOG_VIEWER_MEMBER="group:demo-team@example.com".
LOG_VIEWER_MEMBER="${LOG_VIEWER_MEMBER:-}"
# Optional: a ReasoningEngine id to tail for the post-setup log verification.
AGENT_ENGINE_ID="${AGENT_ENGINE_ID:-}"

echo "=== Setting up Logging Sink → BigQuery ==="
echo "Project: $PROJECT_ID"
echo "Dataset: $DATASET_NAME"

# Enable required APIs
echo "[1/4] Enabling APIs..."
gcloud services enable \
    logging.googleapis.com \
    monitoring.googleapis.com \
    bigquery.googleapis.com \
    --project="$PROJECT_ID"

# Create BigQuery dataset
echo "[2/4] Creating BigQuery dataset..."
bq mk --dataset \
    --project_id="$PROJECT_ID" \
    --description="GEAP Workshop agent traces and eval results" \
    --label solution:geap-tour \
    "$DATASET_NAME" \
    2>/dev/null || echo "  Dataset already exists, skipping."

# Create logging sink for agent traces
echo "[3/4] Creating logging sink..."
gcloud logging sinks create "$SINK_NAME" \
    "bigquery.googleapis.com/projects/${PROJECT_ID}/datasets/${DATASET_NAME}" \
    --project="$PROJECT_ID" \
    --log-filter='resource.type="aiplatform.googleapis.com/ReasoningEngine"' \
    --description="Sink agent traces to BigQuery for evaluation" \
    2>/dev/null || echo "  Sink already exists, skipping."

# Grant the sink writer access to BigQuery
WRITER_IDENTITY=$(gcloud logging sinks describe "$SINK_NAME" \
    --project="$PROJECT_ID" \
    --format="value(writerIdentity)" 2>/dev/null)

if [[ -n "$WRITER_IDENTITY" ]]; then
    bq add-iam-policy-binding \
        --member="$WRITER_IDENTITY" \
        --role="roles/bigquery.dataEditor" \
        "$PROJECT_ID:$DATASET_NAME" \
        2>/dev/null || true
fi

# Grant the observability Viewer roles so operators can read the runtime's
# stdout/stderr logs (Logs Explorer) AND its managed engine metrics + auto "Agent
# Runtime Overview" dashboard (Metrics Explorer) — the docs' required viewing
# steps for logging and monitoring.
echo "[4/4] Granting roles/logging.viewer + roles/monitoring.viewer..."
if [[ -z "$LOG_VIEWER_MEMBER" ]]; then
    ACTIVE_ACCOUNT=$(gcloud config get-value account 2>/dev/null || true)
    if [[ -n "$ACTIVE_ACCOUNT" && "$ACTIVE_ACCOUNT" != "(unset)" ]]; then
        LOG_VIEWER_MEMBER="user:${ACTIVE_ACCOUNT}"
    fi
fi
if [[ -n "$LOG_VIEWER_MEMBER" ]]; then
    for VIEWER_ROLE in roles/logging.viewer roles/monitoring.viewer; do
        gcloud projects add-iam-policy-binding "$PROJECT_ID" \
            --member="$LOG_VIEWER_MEMBER" \
            --role="$VIEWER_ROLE" \
            --condition=None \
            --quiet >/dev/null 2>&1 \
            && echo "  Granted ${VIEWER_ROLE} to ${LOG_VIEWER_MEMBER}" \
            || echo "  Could not grant ${VIEWER_ROLE} to ${LOG_VIEWER_MEMBER} (need resourcemanager.projects.setIamPolicy?)"
    done
else
    echo "  No LOG_VIEWER_MEMBER set and no active gcloud account — skipping."
    echo "  Grant manually: gcloud projects add-iam-policy-binding $PROJECT_ID \\"
    echo "    --member='user:YOU@example.com' --role='roles/logging.viewer'"
    echo "    (repeat with --role='roles/monitoring.viewer')"
fi

echo ""
echo "✓ Logging setup complete"
echo "  Agent traces → BigQuery: ${PROJECT_ID}.${DATASET_NAME}"
echo ""
echo "Verify runtime stdout logs are flowing (needs a deployed engine + recent traffic):"
if [[ -n "$AGENT_ENGINE_ID" ]]; then
    ENGINE_BARE="${AGENT_ENGINE_ID##*/}"
    echo "  Tailing reasoning_engine_stdout for engine ${ENGINE_BARE} (last 5)..."
    gcloud logging read \
        "resource.type=\"aiplatform.googleapis.com/ReasoningEngine\" AND resource.labels.reasoning_engine_id=\"${ENGINE_BARE}\" AND logName:\"reasoning_engine_stdout\"" \
        --project="$PROJECT_ID" --limit=5 --freshness=1h \
        --format="value(timestamp,textPayload)" 2>/dev/null \
        || echo "  (no entries yet — drive some traffic, then re-run)"
else
    echo "  export AGENT_ENGINE_ID=<id> and re-run, or run manually:"
    echo "  gcloud logging read 'resource.type=\"aiplatform.googleapis.com/ReasoningEngine\" AND logName:\"reasoning_engine_stdout\"' \\"
    echo "    --project=$PROJECT_ID --limit=5 --freshness=1h"
fi
