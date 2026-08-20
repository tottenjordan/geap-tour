"""``registry.get_mcp_tools`` resolves MCP toolsets through Agent Registry, with a
loud, robust direct-URL fallback.

The deployed coordinator resolves each MCP server via the Agent Registry control
plane; when that resolution fails it must fall back to the direct Cloud Run URL —
but *visibly* (WARNING, not INFO) and for *both* failure classes ADK can raise
(``RuntimeError`` on HTTP/creds errors, ``ValueError`` on a missing endpoint URI).
These tests use fakes; no GCP, no network.
"""

import logging
import types

import pytest
from google.adk.tools.mcp_tool import McpToolset

import src.registry as registry


class _FakeToolset:
    """Stand-in for an AgentRegistrySingleMcpToolset with tunable timeouts."""

    def __init__(self):
        self._connection_params = types.SimpleNamespace(timeout=5.0, sse_read_timeout=300.0)


def _install_registry(monkeypatch, *, raises=None, toolset=None):
    """Point ``get_registry()`` at a fake whose ``get_mcp_toolset`` is scripted."""

    def _get_mcp_toolset(name):
        if raises is not None:
            raise raises
        return toolset

    fake = types.SimpleNamespace(get_mcp_toolset=_get_mcp_toolset)
    monkeypatch.setattr(registry, "get_registry", lambda: fake)


def test_success_returns_registry_toolset_with_cloud_run_timeouts(monkeypatch):
    """The happy path returns the registry toolset with our Cloud Run timeouts."""
    ts = _FakeToolset()
    _install_registry(monkeypatch, toolset=ts)

    result = registry.get_mcp_tools("search-server")

    assert result is ts
    assert result._connection_params.timeout == registry.MCP_TIMEOUT_SECONDS
    assert result._connection_params.sse_read_timeout == registry.MCP_READ_TIMEOUT_SECONDS


@pytest.mark.parametrize("exc", [RuntimeError("control-plane 403"), ValueError("no endpoint URI")])
def test_fallback_on_resolution_error_returns_direct_toolset_and_warns(monkeypatch, caplog, exc):
    """A resolution failure (RuntimeError *or* ValueError) falls back to a direct
    ``McpToolset`` and logs the fallback at WARNING so operators actually see it."""
    _install_registry(monkeypatch, raises=exc)
    monkeypatch.setattr(registry, "MCP_SERVER_URLS", {"search-server": "https://svc.run.app/mcp"})

    with caplog.at_level(logging.WARNING, logger=registry.log.name):
        result = registry.get_mcp_tools("search-server")

    assert isinstance(result, McpToolset)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "fallback must log at WARNING, not INFO"
    assert any("direct URL" in r.getMessage() for r in warnings)


class TestFallbackMapMatchesTheConfiguredNames:
    """The fallback map must be keyed by the server names the agents actually pass.

    A credential-less environment (CI) reaches Agent Registry, fails on ADC, and
    survives only because ``MCP_SERVER_URLS`` has an entry for the name it was
    called with. That held by accident until the test env vars were applied in a
    *fixture*: modules imported during collection (``src/router/__init__.py``
    imports the router agent) bound ``SEARCH_MCP_SERVER = ""`` while a later
    ``importlib.reload`` of src.config/src.registry re-keyed the map to the real
    names — stranding the empty key and re-raising the ADC error. The defaults now
    land at conftest *import* time, which is what these two assertions pin.
    """

    def test_configured_server_names_are_non_empty(self):
        import src.config as config

        for name in (
            config.SEARCH_MCP_SERVER,
            config.BOOKING_MCP_SERVER,
            config.EXPENSE_MCP_SERVER,
        ):
            assert name, "src.config was imported before the test env defaults were set"

    def test_every_configured_server_name_has_a_fallback_url(self):
        import src.config as config

        for name in (
            config.SEARCH_MCP_SERVER,
            config.BOOKING_MCP_SERVER,
            config.EXPENSE_MCP_SERVER,
        ):
            assert registry.MCP_SERVER_URLS.get(name), f"no direct-URL fallback for {name!r}"


@pytest.mark.parametrize("exc", [RuntimeError("control-plane 403"), ValueError("no endpoint URI")])
def test_fallback_reraises_when_no_mapped_url(monkeypatch, exc):
    """With no direct URL to fall back to, the original error propagates — a
    tool-less coordinator must not be silently accepted."""
    _install_registry(monkeypatch, raises=exc)
    monkeypatch.setattr(registry, "MCP_SERVER_URLS", {})

    with pytest.raises(type(exc)):
        registry.get_mcp_tools("search-server")
