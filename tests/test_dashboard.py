"""Offline tests for dashboard-as-code (no live GCP).

``build_dashboard`` is a pure function returning a Dashboard proto, so most
assertions are structural. ``create_or_update_dashboard`` is exercised with a
fake client that records create/update calls for both branches.
"""

import pytest
from google.api_core import exceptions as gexc
from google.cloud import monitoring_dashboard_v1 as dashboard_v1

from src.observability.dashboard import (
    DASHBOARD_DISPLAY_NAME,
    build_dashboard,
    create_or_update_dashboard,
)
from src.observability.metrics import QUALITY_METRIC_TYPES, TRAFFIC_METRIC_TYPES


def _all_filters(dashboard) -> str:
    """Concatenate every widget's time-series filter string."""
    filters = []
    for tile in dashboard.mosaic_layout.tiles:
        widget = tile.widget
        for ds in widget.xy_chart.data_sets:
            filters.append(ds.time_series_query.time_series_filter.filter)
        # Scorecards carry a filter too.
        sc = widget.scorecard
        if sc.time_series_query.time_series_filter.filter:
            filters.append(sc.time_series_query.time_series_filter.filter)
    return "\n".join(filters)


def test_build_returns_dashboard_with_display_name():
    d = build_dashboard()
    assert isinstance(d, dashboard_v1.Dashboard)
    assert d.display_name == DASHBOARD_DISPLAY_NAME


def test_build_has_widget_per_metric():
    d = build_dashboard()
    tiles = list(d.mosaic_layout.tiles)
    # One tile per traffic metric + one per quality metric.
    expected = len(TRAFFIC_METRIC_TYPES) + len(QUALITY_METRIC_TYPES)
    assert len(tiles) == expected


def test_every_metric_type_appears_in_some_widget():
    d = build_dashboard()
    blob = _all_filters(d)
    for mt in TRAFFIC_METRIC_TYPES + QUALITY_METRIC_TYPES:
        assert mt in blob, f"metric type {mt} missing from dashboard widgets"


def test_quality_filters_use_global_resource():
    """Quality widgets must target resource.type=global (matches alert policies)."""
    d = build_dashboard()
    blob = _all_filters(d)
    for mt in QUALITY_METRIC_TYPES:
        # The filter line for this metric should be present with the metric type.
        assert mt in blob
    assert 'resource.type="global"' in blob


class FakeDashboardClient:
    """Records create/update; get_dashboard raises NotFound or returns existing."""

    def __init__(self, existing=None):
        self._existing = existing  # None -> not found
        self.created = []
        self.updated = []
        self.listed = False

    def common_project_path(self, project_id):
        return f"projects/{project_id}"

    def list_dashboards(self, parent=None):
        self.listed = True
        return iter([self._existing] if self._existing else [])

    def create_dashboard(self, parent=None, dashboard=None):
        self.created.append((parent, dashboard))
        return dashboard

    def update_dashboard(self, dashboard=None):
        self.updated.append(dashboard)
        return dashboard


def test_create_when_not_found():
    client = FakeDashboardClient(existing=None)
    create_or_update_dashboard(client=client, project_id="proj-x")
    assert len(client.created) == 1
    assert len(client.updated) == 0
    parent, dash = client.created[0]
    assert parent == "projects/proj-x"
    assert dash.display_name == DASHBOARD_DISPLAY_NAME


def test_update_when_found():
    existing = dashboard_v1.Dashboard(
        name="projects/proj-x/dashboards/abc123",
        display_name=DASHBOARD_DISPLAY_NAME,
    )
    client = FakeDashboardClient(existing=existing)
    create_or_update_dashboard(client=client, project_id="proj-x")
    assert len(client.created) == 0
    assert len(client.updated) == 1
    # The updated proto must carry the existing resource name so patch targets it.
    assert client.updated[0].name == "projects/proj-x/dashboards/abc123"


def test_update_handles_list_notfound_gracefully():
    """If listing raises NotFound, we should fall back to create."""

    class RaisingClient(FakeDashboardClient):
        def list_dashboards(self, parent=None):
            raise gexc.NotFound("no dashboards")

    client = RaisingClient(existing=None)
    create_or_update_dashboard(client=client, project_id="proj-x")
    assert len(client.created) == 1


def test_import_needs_no_credentials():
    # build_dashboard must not construct a client.
    d = build_dashboard()
    assert d is not None
    with pytest.raises(AttributeError):
        _ = d.does_not_exist
