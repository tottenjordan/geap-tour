#!/usr/bin/env bash
# Generate GEAP workshop architecture diagrams using Paper Banana.
#
# Prerequisites:
#   uv sync --all-groups          # paperbanana>=0.3.0 is a declared dependency;
#                                 # do NOT `pip install` it into a global env.
#   export GOOGLE_API_KEY=...     # or OPENAI_API_KEY. REQUIRED — without it every
#                                 # item fails individually ("GOOGLE_API_KEY not
#                                 # found") while the batch itself still exits 0,
#                                 # so check the per-item table, not $?.
#
# Usage:
#   ./scripts/generate_diagrams.sh            # default: optimize + 3 iterations
#   ./scripts/generate_diagrams.sh --auto     # loop until critic is satisfied
#
# Output:
#   diagrams/outputs/<NN>_<name>.png

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MANIFEST="${REPO_ROOT}/diagrams/batch_manifest.yaml"
OUTPUT_DIR="${REPO_ROOT}/diagrams/outputs"

if [ ! -f "${MANIFEST}" ]; then
  echo "ERROR: Manifest not found at ${MANIFEST}" >&2
  exit 1
fi

# Ensure output directory exists
mkdir -p "${OUTPUT_DIR}"

echo "=== Generating GEAP workshop diagrams ==="
echo "Manifest:   ${MANIFEST}"
echo "Output dir: ${OUTPUT_DIR}"
echo ""

# `uv run`, not a bare `paperbanana`: it is a project dependency living in .venv
# and is not on PATH, so the bare call fails with "command not found" on a clean
# checkout. --no-sync keeps this from re-syncing to the default dependency groups.
uv run --no-sync paperbanana batch \
  --manifest "${MANIFEST}" \
  --output-dir "${OUTPUT_DIR}" \
  --optimize \
  --format png \
  "$@"

echo ""
echo "=== Done ==="
echo "Diagrams saved to: ${OUTPUT_DIR}"
echo ""
echo "To generate a human-readable report:"
echo "  paperbanana batch-report --batch-dir ${OUTPUT_DIR}/batch_* --format markdown"
