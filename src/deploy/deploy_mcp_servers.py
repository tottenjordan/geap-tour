"""Deploy MCP servers to Cloud Run."""

import subprocess
import sys

from src.config import GCP_PROJECT_ID, GCP_REGION, resource_labels_gcloud

SERVERS = [
    {
        "name": "search-mcp",
        "path": "src/mcp_servers/search",
        "port": 8001,
        "env_var": "SEARCH_MCP_URL",
    },
    {
        "name": "booking-mcp",
        "path": "src/mcp_servers/booking",
        "port": 8002,
        "env_var": "BOOKING_MCP_URL",
    },
    {
        "name": "expense-mcp",
        "path": "src/mcp_servers/expense",
        "port": 8003,
        "env_var": "EXPENSE_MCP_URL",
    },
]


def _build_deploy_cmd(server: dict) -> list[str]:
    """Build the `gcloud run deploy` argv for a server, labels included."""
    return [
        "gcloud",
        "run",
        "deploy",
        server["name"],
        "--source",
        server["path"],
        "--region",
        GCP_REGION,
        "--project",
        GCP_PROJECT_ID,
        "--port",
        str(server["port"]),
        "--allow-unauthenticated",
        # Keep one instance warm: a cold start can exceed the coordinator's 60s
        # MCP connect timeout. Cross-instance session drops ("Session terminated")
        # are prevented by stateless HTTP in each server's mcp.run(), not here —
        # so no --max-instances/--session-affinity pinning is needed.
        "--min-instances",
        "1",
        "--labels",
        resource_labels_gcloud(),
        "--quiet",
    ]


def deploy_server(server: dict) -> str:
    """Deploy a single MCP server to Cloud Run and return the service URL."""
    name = server["name"]
    print(f"\n--- Deploying {name} ---")

    cmd = _build_deploy_cmd(server)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR deploying {name}: {result.stderr}", file=sys.stderr)
        raise RuntimeError(f"Failed to deploy {name}")

    url_cmd = [
        "gcloud",
        "run",
        "services",
        "describe",
        name,
        "--region",
        GCP_REGION,
        "--project",
        GCP_PROJECT_ID,
        "--format",
        "value(status.url)",
    ]
    url_result = subprocess.run(url_cmd, capture_output=True, text=True)
    service_url = url_result.stdout.strip()
    print(f"✓ {name} deployed at {service_url}")
    return service_url


def deploy_all_servers(*, write_env: bool = True) -> dict[str, str]:
    """Deploy all MCP servers, record their URLs in ``.env``, return name → URL.

    The write-back is the point. These URLs previously only ever reached stdout, so
    ``SEARCH_MCP_URL`` and friends had to be copy-pasted by hand after every deploy
    — and ``src/config.py`` defaults them to ``http://localhost:800x/mcp``, so a
    missed paste does not fail loudly, it silently points the registry fallback at
    a local port that is not listening.
    """
    from src.deploy.env_file import set_env_var

    urls: dict[str, str] = {}
    for server in SERVERS:
        url = deploy_server(server)
        urls[str(server["name"])] = url
        # Only on a URL we actually got back: recording an empty value would
        # replace a working URL with nothing.
        if write_env and url:
            set_env_var(str(server["env_var"]), f"{url}/mcp")
    return urls


if __name__ == "__main__":
    urls = deploy_all_servers()
    print("\n=== Deployed MCP Server URLs ===")
    for name, url in urls.items():
        print(f"  {name}: {url}/mcp")
