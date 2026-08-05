"""Shared test fixtures — sets safe defaults for env vars required by src.config."""

import os

import pytest

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
