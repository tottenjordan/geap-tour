#!/usr/bin/env bash
# Build + push the eval-runner image to Artifact Registry via Cloud Build
# (no local Docker required). Usage: bash scripts/build_eval_image.sh [tag]
set -euo pipefail
PROJECT=${GCP_PROJECT_ID:-hybrid-vertex}
REGION=${GCP_REGION:-us-central1}
TAG=${1:-latest}
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/geap-eval/eval-runner:${TAG}"
gcloud builds submit --project="$PROJECT" \
    --config=/dev/stdin <<EOF
steps:
- name: gcr.io/cloud-builders/docker
  args: ['build','-f','docker/eval/Dockerfile','-t','${IMAGE}','.']
images: ['${IMAGE}']
EOF
echo "$IMAGE"
