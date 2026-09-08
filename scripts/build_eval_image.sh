#!/usr/bin/env bash
# Build + push the eval-runner image to Artifact Registry via Cloud Build
# (no local Docker required). Usage: bash scripts/build_eval_image.sh [tag]

# Loads .env and provides PROJECT_ID / REGION / project_number / require_var.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/config.sh"
set -euo pipefail
PROJECT="${PROJECT_ID}"
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
