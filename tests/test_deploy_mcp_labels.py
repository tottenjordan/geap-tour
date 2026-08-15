"""The Cloud Run MCP deploy command carries the default resource label."""

import src.config as cfg
from src.deploy.deploy_mcp_servers import _build_deploy_cmd


def test_mcp_cmd_has_labels():
    cmd = _build_deploy_cmd({"name": "search-mcp", "path": "p", "port": 8001})
    assert "--labels" in cmd
    assert cfg.resource_labels_gcloud() in cmd


def test_mcp_cmd_keeps_one_instance_warm():
    """--min-instances 1 keeps a warm instance so a cold start can't blow the
    coordinator's 60s MCP connect timeout. (Stateless HTTP — set in each
    server's mcp.run — is what actually prevents 'Session terminated'; this is
    just latency insurance.)"""
    cmd = _build_deploy_cmd({"name": "search-mcp", "path": "p", "port": 8001})
    assert "--min-instances" in cmd
    assert cmd[cmd.index("--min-instances") + 1] == "1"
