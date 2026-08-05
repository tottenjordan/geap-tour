# Eval-runner image: one image backs every KFP pipeline component.
# Installs the project as a wheel (hatchling packages src/, including the
# src/eval/evalsets/*.json and scenarios/*.json data files the eval cases need).
# Pinned to Python 3.12 to match requires-python (>=3.12).
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 GOOGLE_GENAI_USE_VERTEXAI=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
# Sane non-secret defaults; deployment-specific values (MCP servers, engine IDs)
# are injected per-task at run time via KFP .set_env_variable(...).
ENV GCP_PROJECT_ID=hybrid-vertex GCP_REGION=us-central1 \
    GCP_STAGING_BUCKET=geap-tour-staging-v2 EVAL_OUTPUT_DIR=/tmp/eval_outputs
