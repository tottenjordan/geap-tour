"""The Cloud Run MCP deploy command carries the default resource label."""

import src.config as cfg
from src.deploy.deploy_mcp_servers import _build_deploy_cmd


def test_mcp_cmd_has_labels():
    cmd = _build_deploy_cmd({"name": "search-mcp", "path": "p", "port": 8001})
    assert "--labels" in cmd
    assert cfg.resource_labels_gcloud() in cmd
