"""Static guard: the deployed engine bundles OTel instrumentation packages.

Agent Engine auto-enables telemetry, but without the OpenTelemetry *instrumentation*
packages installed in the runtime the emitted spans carry no ``gen_ai.*`` prompt/
response attributes. Online Evaluators score ``{prompt}``/``{response}`` extracted
from those gen_ai spans, so a missing instrumentation dep silently yields **zero**
eval results even though the evaluator is ACTIVE at 100% sampling.

The critical package is ``opentelemetry-instrumentation-google-genai`` (it
instruments the google-genai SDK calls the agents make); grpc/httpx add the
network spans. We assert by reading the file text (rather than importing, which
would require live GCP env) — matching the pattern in ``test_labels_wired.py``.
"""

import pathlib

REQUIRED_INSTRUMENTATION = [
    "opentelemetry-instrumentation-google-genai",
    "opentelemetry-instrumentation-grpc",
    "opentelemetry-instrumentation-httpx",
]


def test_deploy_requirements_include_otel_instrumentation():
    text = pathlib.Path("src/deploy/deploy_agents.py").read_text()
    for pkg in REQUIRED_INSTRUMENTATION:
        assert pkg in text, f"{pkg} missing from deploy_agents REQUIREMENTS"


def test_genai_instrumentation_is_a_project_dependency():
    # Parity: keep it in pyproject so uv.lock pins it and local runs trace too.
    text = pathlib.Path("pyproject.toml").read_text()
    assert "opentelemetry-instrumentation-google-genai" in text
