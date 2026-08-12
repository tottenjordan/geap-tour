"""Fetch a Cloud Trace by id and print its span names + key attributes.

A small demo/debugging fallback for inspecting agent-side traces (e.g. the
``router.route`` span) without leaving the terminal. Import-safe: if the
Cloud Trace client library is unavailable it prints an install hint instead of
crashing at import.

Usage:
  uv run python -m src.observability.fetch_trace <TRACE_ID>
  uv run python -m src.observability.fetch_trace <TRACE_ID> --project my-proj
"""

from __future__ import annotations

import argparse

from src.config import GCP_PROJECT_ID

try:
    from google.cloud import trace_v1

    _TRACE_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - exercised only when lib is absent
    trace_v1 = None  # type: ignore[assignment]
    _TRACE_IMPORT_ERROR = exc


def fetch_trace(trace_id: str, project: str = GCP_PROJECT_ID) -> int:
    """Fetch and print one trace. Returns a process exit code."""
    if trace_v1 is None:
        print(
            "google-cloud-trace client not available "
            f"({_TRACE_IMPORT_ERROR}).\n"
            "Install it with: uv add google-cloud-trace"
        )
        return 1

    client = trace_v1.TraceServiceClient()
    trace = client.get_trace(project_id=project, trace_id=trace_id)

    print(f"Trace {trace.trace_id} ({len(trace.spans)} spans)")
    for span in trace.spans:
        print(f"\n- {span.name}  (span_id={span.span_id})")
        labels = dict(span.labels)
        for key in sorted(labels):
            print(f"    {key} = {labels[key]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace_id", help="Cloud Trace id to fetch")
    parser.add_argument(
        "--project",
        default=GCP_PROJECT_ID,
        help=f"GCP project id (default: {GCP_PROJECT_ID})",
    )
    args = parser.parse_args()
    return fetch_trace(args.trace_id, args.project)


if __name__ == "__main__":
    raise SystemExit(main())
