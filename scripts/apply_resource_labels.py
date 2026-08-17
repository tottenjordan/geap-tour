"""Idempotently stamp RESOURCE_LABELS onto assets already in the project.

Retro-fits the default ``{"solution": "geap-tour"}`` label onto the resources
provisioned before labelling was wired into the creation code — no engine
re-upload. Re-running is a no-op: every PATCH merges labels, so already-tagged
resources stay tagged.

Each section is independently try-wrapped so one failure (e.g. a preview API
that rejects ``labels``) never aborts the rest. Run it with::

    uv run python -m scripts.apply_resource_labels

The pure request builders (``_engine_patch`` / ``_armor_patch``) are unit-tested
offline; the section functions drive live clients and are exercised on GCP.
"""

from __future__ import annotations

import os
import subprocess

from src.config import (
    BQ_EVAL_DATASET,
    GCP_PROJECT_ID,
    GCP_REGION,
    GCP_STAGING_BUCKET,
    RESOURCE_LABELS,
)

# The 7 deployed reasoning engines, by their .env keys. AGENT_ENGINE_ID is a
# pointer (may alias the coordinator) — the coordinator itself is relabeled via
# COORDINATOR_AGENT_ID, so it is intentionally omitted here to avoid a dup PATCH.
ENGINE_ENV_KEYS = [
    "COORDINATOR_AGENT_ID",
    "ROUTER_ENGINE_ID",
    "FLASH_ENGINE_ID",
    "LITE_ENGINE_ID",
    "PRO_ENGINE_ID",
    "SONNET_ENGINE_ID",
    "OPUS_ENGINE_ID",
]

ARMOR_TEMPLATE_IDS = ["geap-workshop-prompt", "geap-workshop-response"]
MCP_SERVICES = ["search-mcp", "booking-mcp", "expense-mcp"]


# ── Pure request builders (unit-tested, no network) ──────────────────────────


def _region_from_resource(resource_name: str) -> str:
    """Extract the `locations/<region>` segment from a resource name."""
    parts = resource_name.split("/")
    if "locations" in parts:
        return parts[parts.index("locations") + 1]
    return GCP_REGION


def _engine_patch(resource_name: str, labels: dict) -> tuple[str, dict]:
    """(url, body) for a reasoningEngine labels PATCH."""
    region = _region_from_resource(resource_name)
    url = f"https://{region}-aiplatform.googleapis.com/v1/{resource_name}?updateMask=labels"
    return url, {"labels": dict(labels)}


def _armor_patch(resource_name: str, labels: dict) -> tuple[str, dict]:
    """(url, body) for a Model Armor template labels PATCH."""
    region = _region_from_resource(resource_name)
    url = f"https://modelarmor.{region}.rep.googleapis.com/v1/{resource_name}?updateMask=labels"
    return url, {"labels": dict(labels)}


# ── Live helpers ─────────────────────────────────────────────────────────────


def _authed_session():
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    creds, _ = google.auth.default()
    return AuthorizedSession(creds)


def _rest_patch(session, url: str, body: dict, label: str) -> None:
    """GET-merge-PATCH: preserve any existing labels, then add ours."""
    get_url = url.split("?", 1)[0]
    existing = {}
    try:
        resp = session.get(get_url)
        if resp.ok:
            existing = resp.json().get("labels", {}) or {}
    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"  ! could not read existing labels for {label}: {e}")

    merged = {**existing, **body["labels"]}
    resp = session.patch(url, json={"labels": merged})
    if resp.ok:
        print(f"  patched: {label}")
    else:
        print(f"  ! failed {label}: {resp.status_code} {resp.text[:200]}")


# ── Section functions (each try-wrapped in main) ─────────────────────────────


def label_engines(session) -> None:
    print("Agent engines:")
    for key in ENGINE_ENV_KEYS:
        engine_id = os.environ.get(key)
        if not engine_id:
            print(f"  - {key} not set, skipping")
            continue
        resource = f"projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/reasoningEngines/{engine_id}"
        url, body = _engine_patch(resource, RESOURCE_LABELS)
        _rest_patch(session, url, body, f"{key}={engine_id}")


def label_armor_templates(session) -> None:
    print("Model Armor templates:")
    for tid in ARMOR_TEMPLATE_IDS:
        resource = f"projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/templates/{tid}"
        url, body = _armor_patch(resource, RESOURCE_LABELS)
        _rest_patch(session, url, body, tid)


def label_bigquery_dataset() -> None:
    print("BigQuery dataset:")
    try:
        from google.cloud import bigquery

        client = bigquery.Client(project=GCP_PROJECT_ID)
        ds = client.get_dataset(f"{GCP_PROJECT_ID}.{BQ_EVAL_DATASET}")
        ds.labels = {**(ds.labels or {}), **RESOURCE_LABELS}
        client.update_dataset(ds, ["labels"])
        print(f"  patched: {BQ_EVAL_DATASET}")
    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"  ! failed {BQ_EVAL_DATASET}: {e}")


def label_alert_policies() -> None:
    print("Alert policies:")
    try:
        from google.cloud import monitoring_v3
        from google.protobuf import field_mask_pb2

        client = monitoring_v3.AlertPolicyServiceClient()
        parent = f"projects/{GCP_PROJECT_ID}"
        for p in client.list_alert_policies(name=parent):
            if "GEAP Workshop" not in p.display_name:
                continue
            merged = {**dict(p.user_labels), **RESOURCE_LABELS}
            if dict(p.user_labels) == merged:
                print(f"  ok (already labeled): {p.display_name}")
                continue
            p.user_labels.clear()
            p.user_labels.update(merged)
            client.update_alert_policy(
                alert_policy=p,
                update_mask=field_mask_pb2.FieldMask(paths=["user_labels"]),
            )
            print(f"  patched: {p.display_name}")
    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"  ! failed alert policies: {e}")


def label_dashboard() -> None:
    print("Dashboard:")
    try:
        from google.cloud import monitoring_dashboard_v1 as dashboard_v1

        from src.observability.dashboard import _find_existing

        client = dashboard_v1.DashboardsServiceClient()
        parent = client.common_project_path(GCP_PROJECT_ID)
        existing = _find_existing(client, parent)
        if existing is None:
            print("  - dashboard not found, skipping")
            return
        merged = {**dict(existing.labels), **RESOURCE_LABELS}
        existing.labels.clear()
        existing.labels.update(merged)
        client.update_dashboard(request={"dashboard": existing})
        print(f"  patched: {existing.display_name}")
    except Exception as e:  # pylint: disable=broad-exception-caught
        print(f"  ! failed dashboard: {e}")


def _gcloud_update_labels(cmd: list[str], label: str) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"  patched: {label}")
    else:
        print(f"  ! failed {label}: {result.stderr.strip()[:200]}")


def label_cloud_run_services() -> None:
    print("Cloud Run MCP services:")
    from src.config import resource_labels_gcloud

    for name in MCP_SERVICES:
        _gcloud_update_labels(
            [
                "gcloud",
                "run",
                "services",
                "update",
                name,
                "--region",
                GCP_REGION,
                "--project",
                GCP_PROJECT_ID,
                "--update-labels",
                resource_labels_gcloud(),
                "--quiet",
            ],
            name,
        )


def label_gcs_bucket() -> None:
    print("GCS staging bucket:")
    # `gcloud storage buckets update --update-labels` 400s on this project;
    # `gsutil label ch -l k:v` merges labels reliably and is idempotent.
    ch_flags = []
    for k, v in RESOURCE_LABELS.items():
        ch_flags += ["-l", f"{k}:{v}"]
    _gcloud_update_labels(
        ["gsutil", "label", "ch", *ch_flags, f"gs://{GCP_STAGING_BUCKET}"],
        GCP_STAGING_BUCKET,
    )


def main() -> None:
    print(f"Applying {RESOURCE_LABELS} to existing assets in {GCP_PROJECT_ID}\n")
    session = _authed_session()

    for section in (
        lambda: label_engines(session),
        lambda: label_armor_templates(session),
        label_bigquery_dataset,
        label_alert_policies,
        label_dashboard,
        label_cloud_run_services,
        label_gcs_bucket,
    ):
        try:
            section()
        except Exception as e:  # pylint: disable=broad-exception-caught
            print(f"  ! section error: {e}")
        print()

    print("Done.")


if __name__ == "__main__":
    main()
