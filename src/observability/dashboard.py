"""Dashboard-as-code for the GEAP observability demo.

Builds a Cloud Monitoring dashboard proto whose widgets chart the custom metrics
emitted by ``src/observability/metrics.py``:

* traffic: request latency (p50/p95), achieved QPS, error rate, injected count
* quality: the coordinator ``agent_eval/*`` scores (1-5 rubric axis, offline snapshot)
* online quality: the ``agent_online_eval/*`` scores (1-5 axis, continuous
  live-traffic sampling — sidesteps the platform-blocked native Online Evaluators)
* router: the ``agent_router/*`` efficiency scores (native units: %, ms)

``build_dashboard()`` is a pure function (no API calls) so tests assert on the
proto directly. ``create_or_update_dashboard()`` is an idempotent apply: it finds
an existing board by display name and patches it, else creates a new one. The
``DashboardsServiceClient`` is constructed lazily and injectable for tests.

Run it with::

    uv run python -m src.observability.dashboard
"""

from __future__ import annotations

from google.api_core import exceptions as gexc
from google.cloud import monitoring_dashboard_v1 as dashboard_v1

from src.config import GCP_PROJECT_ID, RESOURCE_LABELS
from src.observability.metrics import (
    ONLINE_QUALITY_METRIC_TYPES,
    QUALITY_METRIC_TYPES,
    ROUTER_METRIC_TYPES,
    TRAFFIC_METRIC_TYPES,
)

DASHBOARD_DISPLAY_NAME = "GEAP Workshop: Agent Observability"

# Friendly widget titles keyed by metric type. Anything not listed falls back to
# a title derived from the metric type's trailing segment.
_TITLES = {
    "custom.googleapis.com/agent_traffic/request_latency_p50": "Request Latency p50 (s)",
    "custom.googleapis.com/agent_traffic/request_latency_p95": "Request Latency p95 (s)",
    "custom.googleapis.com/agent_traffic/error_rate": "Error Rate",
    "custom.googleapis.com/agent_traffic/qps": "Achieved QPS",
    "custom.googleapis.com/agent_traffic/injected": "Injected / Blocked Queries",
    "custom.googleapis.com/agent_eval/helpfulness": "Eval: Helpfulness",
    "custom.googleapis.com/agent_eval/tool_use_accuracy": "Eval: Tool-Use Accuracy",
    "custom.googleapis.com/agent_eval/policy_compliance": "Eval: Policy Compliance",
    "custom.googleapis.com/agent_eval/tool_faithfulness": "Eval: Tool-Call Faithfulness",
    "custom.googleapis.com/agent_online_eval/helpfulness": "Online Eval: Helpfulness",
    "custom.googleapis.com/agent_online_eval/tool_use_accuracy": "Online Eval: Tool-Use Accuracy",
    "custom.googleapis.com/agent_online_eval/policy_compliance": "Online Eval: Policy Compliance",
    "custom.googleapis.com/agent_online_eval/tool_faithfulness": "Online Eval: Tool-Call Faithfulness",
    "custom.googleapis.com/agent_router/routing_accuracy_pct": "Router: Routing Accuracy (%)",
    "custom.googleapis.com/agent_router/cost_savings_pct": ("Router: Cost Savings vs All-Opus (%)"),
    "custom.googleapis.com/agent_router/classifier_latency_ms": ("Router: Classifier Latency (ms)"),
}

_TILE_WIDTH = 6
_TILE_HEIGHT = 4
_COLUMNS = 12


def _title_for(metric_type: str) -> str:
    return _TITLES.get(metric_type, metric_type.rsplit("/", 1)[-1])


def _xy_widget(metric_type: str, breakdown_label: str | None = None) -> dashboard_v1.Widget:
    """A line-chart widget plotting the mean of a single custom metric type.

    When ``breakdown_label`` is set (e.g. ``"model"``), the series are grouped by
    that metric label and reduced per group, so a bake-off's two deployments each
    draw their own line instead of collapsing into a single mean.
    """
    aggregation = dashboard_v1.Aggregation(
        alignment_period={"seconds": 60},
        per_series_aligner=dashboard_v1.Aggregation.Aligner.ALIGN_MEAN,
    )
    title = _title_for(metric_type)
    if breakdown_label is not None:
        aggregation.cross_series_reducer = dashboard_v1.Aggregation.Reducer.REDUCE_MEAN
        aggregation.group_by_fields = [f"metric.label.{breakdown_label}"]
        title = f"{title} (by {breakdown_label})"
    query = dashboard_v1.TimeSeriesQuery(
        time_series_filter=dashboard_v1.TimeSeriesFilter(
            filter=f'metric.type="{metric_type}" AND resource.type="global"',
            aggregation=aggregation,
        )
    )
    return dashboard_v1.Widget(
        title=title,
        xy_chart=dashboard_v1.XyChart(
            data_sets=[
                dashboard_v1.XyChart.DataSet(
                    time_series_query=query,
                    plot_type=dashboard_v1.XyChart.DataSet.PlotType.LINE,
                )
            ]
        ),
    )


def build_dashboard() -> dashboard_v1.Dashboard:
    """Return the Dashboard proto (no API calls).

    Each metric gets an aggregate (all-series-mean) widget. Traffic, quality, and
    online-quality metrics additionally get a per-``model`` breakdown widget so a
    bake-off's two coordinator deployments render as separate lines; router
    metrics don't (the router is a single agent, not a per-model comparison).
    """
    # (metric_type, breakdown_label) — None means the plain aggregate widget.
    specs: list[tuple[str, str | None]] = [
        (mt, None)
        for mt in list(TRAFFIC_METRIC_TYPES)
        + list(QUALITY_METRIC_TYPES)
        + list(ONLINE_QUALITY_METRIC_TYPES)
        + list(ROUTER_METRIC_TYPES)
    ]
    specs += [
        (mt, "model")
        for mt in list(TRAFFIC_METRIC_TYPES)
        + list(QUALITY_METRIC_TYPES)
        + list(ONLINE_QUALITY_METRIC_TYPES)
    ]

    tiles = []
    for i, (metric_type, breakdown) in enumerate(specs):
        tiles.append(
            dashboard_v1.MosaicLayout.Tile(
                x_pos=(i % 2) * _TILE_WIDTH,
                y_pos=(i // 2) * _TILE_HEIGHT,
                width=_TILE_WIDTH,
                height=_TILE_HEIGHT,
                widget=_xy_widget(metric_type, breakdown_label=breakdown),
            )
        )

    return dashboard_v1.Dashboard(
        display_name=DASHBOARD_DISPLAY_NAME,
        mosaic_layout=dashboard_v1.MosaicLayout(columns=_COLUMNS, tiles=tiles),
        labels=dict(RESOURCE_LABELS),
    )


def _find_existing(client, parent: str) -> dashboard_v1.Dashboard | None:
    """Return the existing GEAP dashboard by display name, or None."""
    try:
        for existing in client.list_dashboards(parent=parent):
            if existing.display_name == DASHBOARD_DISPLAY_NAME:
                return existing
    except gexc.NotFound:
        return None
    return None


def _console_link(dashboard_name: str, project_id: str) -> str:
    """Build the Cloud Console deep-link for a dashboard resource name."""
    dashboard_id = dashboard_name.rsplit("/", 1)[-1] if dashboard_name else ""
    return (
        f"https://console.cloud.google.com/monitoring/dashboards/builder/"
        f"{dashboard_id}?project={project_id}"
    )


def create_or_update_dashboard(client=None, project_id: str = GCP_PROJECT_ID):
    """Idempotently create or patch the observability dashboard.

    Finds an existing board by display name: patches it if present, else creates
    a new one. The client is constructed lazily so importing this module needs no
    credentials. Prints the console deep-link.
    """
    if client is None:
        client = dashboard_v1.DashboardsServiceClient()

    parent = client.common_project_path(project_id)
    desired = build_dashboard()
    existing = _find_existing(client, parent)

    if existing is not None:
        desired.name = existing.name
        # The patch API rejects an update without the current etag ("400 Update
        # Dashboard should specify a non empty etag"), so carry it over.
        desired.etag = existing.etag
        result = client.update_dashboard(request={"dashboard": desired})
        action = "Updated"
    else:
        result = client.create_dashboard(parent=parent, dashboard=desired)
        action = "Created"

    name = getattr(result, "name", "") or desired.name
    link = _console_link(name, project_id)
    print(f"✓ {action} dashboard: {DASHBOARD_DISPLAY_NAME}")
    print(f"  {name}")
    print(f"  {link}")
    return result


if __name__ == "__main__":
    create_or_update_dashboard()
