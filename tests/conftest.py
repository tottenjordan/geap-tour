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

collect_ignore = [
    module for module, dep in _OPTIONAL_DEP_MODULES.items() if importlib.util.find_spec(dep) is None
]

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
    "GOOGLE_GENAI_USE_VERTEXAI": "1",
}


@pytest.fixture(autouse=True, scope="session")
def set_test_env():
    """Set minimal env vars for structural tests. Only sets vars not already defined."""
    original = {}
    for key, value in _TEST_ENV_DEFAULTS.items():
        if key not in os.environ:
            original[key] = None
            os.environ[key] = value
        else:
            original[key] = os.environ[key]
    yield
    for key, value in original.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
