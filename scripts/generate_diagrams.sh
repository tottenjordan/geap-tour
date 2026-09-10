#!/usr/bin/env bash
# Generate GEAP workshop architecture diagrams using Paper Banana.
#
# Prerequisites:
#   uv sync --all-groups          # paperbanana>=0.3.0 is a declared dependency;
#                                 # do NOT `pip install` it into a global env.
#   GOOGLE_API_KEY                # or OPENAI_API_KEY. REQUIRED. Read from .env or
#                                 # the environment (env wins). Without it EVERY
#                                 # item fails individually ("GOOGLE_API_KEY not
#                                 # found") while the batch still exits 0 — so this
#                                 # script checks for it up front rather than
#                                 # letting a green exit code hide a failed run.
#
# Usage:
#   ./scripts/generate_diagrams.sh            # default: optimize + 3 iterations
#   ./scripts/generate_diagrams.sh --auto     # loop until critic is satisfied
#
# Output:
#   diagrams/outputs/<NN>_<name>.png

# Sources .env the same way every other script here does (caller env wins over the
# file). This script used to claim the key could live "in your environment (or .env
# file)" while reading only the environment, so a key sitting in .env did nothing.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/config.sh"

MANIFEST="${REPO_ROOT}/diagrams/batch_manifest.yaml"
OUTPUT_DIR="${REPO_ROOT}/diagrams/outputs"

if [ ! -f "${MANIFEST}" ]; then
  echo "ERROR: Manifest not found at ${MANIFEST}" >&2
  exit 1
fi

# Fail fast on a missing key. paperbanana fails each ITEM and still exits 0, so
# without this the run looks successful and quietly renders nothing — which is
# how a broken generator went unnoticed between 2026-08-22 and 2026-09-10.
if [ -z "${GOOGLE_API_KEY:-}" ] && [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "ERROR: neither GOOGLE_API_KEY nor OPENAI_API_KEY is set (checked env and .env)." >&2
  echo "       Every diagram would fail individually while this script exited 0." >&2
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
#
# Forcing both genai backend flags to `false` is load-bearing and counter-intuitive.
#
# .env sets GOOGLE_GENAI_USE_VERTEXAI=1 and GOOGLE_GENAI_USE_ENTERPRISE=1 because
# every OTHER caller in this repo talks to Vertex with ADC. paperbanana instead
# authenticates to the Gemini Developer API with GOOGLE_API_KEY, and google-genai
# reads those two flags to choose its backend — so the client silently switches to
# Vertex mode, ignores the key, and every diagram dies on
# `ClientError 401 UNAUTHENTICATED` while the key itself is perfectly valid.
#
# They must be SET TO false, not unset: paperbanana calls `load_dotenv()` itself
# (cli.py), which re-reads .env and would put the 1s straight back. load_dotenv
# does not override a name already present in the environment, so an explicit
# false survives it — `env -u` does not.
#
# Verified 2026-09-10: the same key returns HTTP 200 from
# generativelanguage.googleapis.com, and a genai client built after load_dotenv()
# with both flags false reports `vertexai = False` and completes a call.
GOOGLE_GENAI_USE_VERTEXAI=false GOOGLE_GENAI_USE_ENTERPRISE=false \
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
