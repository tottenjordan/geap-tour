"""Shared test fixtures — sets safe defaults for env vars required by src.config."""

import importlib.util
import os

import pytest

# Optional-dependency test modules. These import heavy optional deps (the `doe`
# and `pipelines` dependency groups) that a bare `uv run pytest` doesn't sync,
# which otherwise surfaces as *collection errors* that interrupt the ENTIRE run
# ("Interrupted: N errors during collection"). CI installs these groups
# (`uv sync --group doe --group pipelines`), so nothing is skipped there; a bare
# local run degrades to clean skips instead of aborting the whole suite. Each
# module is gated on the top-level import that pulls its group in.
_OPTIONAL_DEP_MODULES = {
    "test_doe_design.py": "pyDOE3",
    "test_doe_launch.py": "pyDOE3",
    "test_doe_run.py": "pyDOE3",
    "test_pipeline.py": "kfp",
    "test_optimize_pipeline.py": "kfp",
}

_IGNORED_MODULES = [
    module for module, dep in _OPTIONAL_DEP_MODULES.items() if importlib.util.find_spec(dep) is None
]

collect_ignore = list(_IGNORED_MODULES)


def _missing_deps() -> list[str]:
    return sorted({_OPTIONAL_DEP_MODULES[m] for m in _IGNORED_MODULES})


def pytest_report_header(config):  # pytest hook signature; `config` is unused
    """Say out loud that the suite shrank.

    `collect_ignore` makes modules VANISH — not skip. There is no `s` in the
    progress output, no skip count in the summary, and no warning: the run simply
    reports a smaller number that looks like a pass. Measured exposure is 39 tests
    (1959 -> 1920), which is how CLAUDE.md's warning came to be written.

    A shrinking green suite is the failure mode this repo keeps finding elsewhere
    (an alert on a series with no writer; a plugin reported ACTIVE because a flag
    was set). Reporting it in the header costs nothing and makes it unmissable.

    NOTE: pytest suppresses report headers under `-q`. That is why the CI gate below
    is a hard error and not merely this banner — the short form is exactly the one
    people run.
    """
    if not _IGNORED_MODULES:
        return None
    return [
        f"WARNING: {len(_IGNORED_MODULES)} test modules NOT COLLECTED "
        f"(missing: {', '.join(_missing_deps())})",
        "  " + ", ".join(sorted(_IGNORED_MODULES)),
        "  These are ignored, not skipped — the totals below are silently short.",
        "  Run `uv sync --all-groups` to collect the whole suite.",
    ]


def pytest_sessionstart(session):  # pytest hook signature; `session` is unused
    """In CI, a partial suite is an error rather than a smaller pass.

    `.github/workflows/tests.yaml` syncs `dev + pipelines + doe`, so nothing should
    be ignored there. Nothing verified that. Drop one `--group` from that line and
    39 tests disappear behind a green check — the workflow has no idea what it was
    supposed to collect.

    Set ALLOW_PARTIAL_TEST_RUN=1 to opt out deliberately.
    """
    if not _IGNORED_MODULES or os.environ.get("ALLOW_PARTIAL_TEST_RUN"):
        return
    if not os.environ.get("CI"):
        return  # local runs degrade to the loud header above
    raise pytest.UsageError(
        f"CI collected a PARTIAL suite: {len(_IGNORED_MODULES)} modules ignored "
        f"({', '.join(sorted(_IGNORED_MODULES))}) because these dependency groups "
        f"are missing: {', '.join(_missing_deps())}. A green run here would be "
        "reporting on a suite that silently shrank. Fix the `uv sync --group ...` "
        "line, or set ALLOW_PARTIAL_TEST_RUN=1 if this is intentional."
    )


@pytest.fixture
def span_exporter():
    """Install a fresh in-memory TracerProvider, then restore the previous one.

    Restoring matters: the rest of the suite relies on the default (no-op)
    provider, which is what makes ``src.observability.tracing`` transparent
    outside a deployed engine.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.util._once import Once

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    saved_provider = trace._TRACER_PROVIDER
    saved_once = trace._TRACER_PROVIDER_SET_ONCE
    # Reset the "set once" guard so a fresh provider can be installed.
    trace._TRACER_PROVIDER_SET_ONCE = Once()
    trace._TRACER_PROVIDER = None
    trace.set_tracer_provider(provider)
    try:
        yield exporter
    finally:
        trace._TRACER_PROVIDER = saved_provider
        trace._TRACER_PROVIDER_SET_ONCE = saved_once


_TEST_ENV_DEFAULTS = {
    "GCP_PROJECT_ID": "test-project",
    "GCP_REGION": "us-central1",
    "GCP_STAGING_BUCKET": "test-bucket",
    "SEARCH_MCP_SERVER": "projects/test-project/locations/us-central1/mcpServers/test-search",
    "BOOKING_MCP_SERVER": "projects/test-project/locations/us-central1/mcpServers/test-booking",
    "EXPENSE_MCP_SERVER": "projects/test-project/locations/us-central1/mcpServers/test-expense",
    "SEARCH_MCP_URL": "http://localhost:8001/mcp",
    "BOOKING_MCP_URL": "http://localhost:8002/mcp",
    "EXPENSE_MCP_URL": "http://localhost:8003/mcp",
    # ADK 2.7.1 / google-genai 2.19.0 renamed this; both are set so the suite
    # exercises the same pair the deployed engines get and stays quiet under
    # ADK's DeprecationWarning for the old spelling.
    "GOOGLE_GENAI_USE_ENTERPRISE": "1",
    "GOOGLE_GENAI_USE_VERTEXAI": "1",
}


# Applied at conftest IMPORT time, NOT from a fixture. pytest imports conftest.py
# before it collects the test modules, and collection imports src.config
# transitively (src/router/__init__.py imports the router agent, which binds
# SEARCH_MCP_SERVER et al. at import). A session fixture runs *after* collection,
# so on a machine without a .env (CI) those modules bound empty server names while
# a later importlib.reload of src.config/src.registry (tests/test_prompt_variant.py)
# re-keyed src.registry's direct-URL fallback map to these real names — the empty
# key vanished and a credential-less get_mcp_tools re-raised the ADC error instead
# of falling back. Setting the defaults here closes that window: every module sees
# the same names, whenever it is imported or reloaded.
# setdefault, so an exported env var still wins — but note these now also take
# precedence over a local .env, since src.config's load_dotenv() does not override
# what is already in os.environ. That is deliberate: it makes a local run and CI
# resolve the same names.
for _key, _value in _TEST_ENV_DEFAULTS.items():
    os.environ.setdefault(_key, _value)
