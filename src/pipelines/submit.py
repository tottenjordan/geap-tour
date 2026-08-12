"""Compile and submit the GEAP eval pipeline to Vertex AI Managed Pipelines.

Manual trigger only (no scheduler). Examples::

    # Reuse an existing engine, skip traffic (fastest smoke path)
    uv run python -m src.pipelines.submit --agent-id "$AGENT_ENGINE_ID" --skip-traffic

    # Full parity with a fresh temp deploy (auto-cleaned by the exit handler)
    uv run python -m src.pipelines.submit --agent-module coordinator_agent

All eval compute runs on Vertex; this CLI only compiles the spec and submits
the ``PipelineJob``.
"""

import argparse
import os
import uuid

from google.cloud import aiplatform
from kfp import compiler

from src.config import GCP_PROJECT_ID, GCP_REGION, GCP_STAGING_BUCKET, RESOURCE_LABELS
from src.pipelines.eval_pipeline import eval_pipeline

# Reuse the project compute SA (already provisioned; see docs note on
# least-privilege follow-up).
DEFAULT_SERVICE_ACCOUNT = "934903580331-compute@developer.gserviceaccount.com"
# Compiled specs are build artifacts (gitignored), kept out of the repo root.
# DOE runs override --spec-path to nest under doe_runs/<experiment_id>/.
PIPELINE_SPEC = "build/pipeline_specs/eval_pipeline.yaml"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", default="", help="Existing engine ID; empty deploys a temp engine")
    parser.add_argument("--agent-module", default="coordinator_agent")
    parser.add_argument("--threshold", type=float, default=3.0)
    parser.add_argument("--skip-traffic", action="store_true")
    parser.add_argument("--traffic-count", type=int, default=2)
    parser.add_argument("--scenario-count", type=int, default=5)
    parser.add_argument("--max-turns", type=int, default=3)
    parser.add_argument("--service-account", default=DEFAULT_SERVICE_ACCOUNT)
    # DOE bookkeeping: tag the run so the report component writes to a
    # deterministic per-design-point GCS prefix the harvester can find.
    parser.add_argument("--experiment-id", default="")
    parser.add_argument("--design-point", default="")
    # Unique spec path per design point avoids the shared-file compile race when
    # the DOE launcher submits many points concurrently.
    parser.add_argument("--spec-path", default=PIPELINE_SPEC)
    args = parser.parse_args()

    # A fresh deploy gets a unique display_name so the exit-handler cleanup can
    # find and delete exactly this run's temp engine. Reuse runs skip cleanup.
    temp_display_name = "" if args.agent_id else f"geap-eval-temp-{uuid.uuid4().hex[:12]}"

    os.makedirs(os.path.dirname(args.spec_path) or ".", exist_ok=True)
    compiler.Compiler().compile(eval_pipeline, args.spec_path)

    aiplatform.init(
        project=GCP_PROJECT_ID,
        location=GCP_REGION,
        staging_bucket=f"gs://{GCP_STAGING_BUCKET}",
    )
    job = aiplatform.PipelineJob(
        display_name="geap-eval",
        template_path=args.spec_path,
        pipeline_root=f"gs://{GCP_STAGING_BUCKET}/pipeline-root",
        parameter_values={
            "agent_id": args.agent_id,
            "agent_module": args.agent_module,
            "threshold": args.threshold,
            "skip_traffic": args.skip_traffic,
            "traffic_count": args.traffic_count,
            "scenario_count": args.scenario_count,
            "max_turns": args.max_turns,
            "temp_display_name": temp_display_name,
            "experiment_id": args.experiment_id,
            "design_point": args.design_point,
        },
        labels=dict(RESOURCE_LABELS),
    )
    job.submit(service_account=args.service_account)  # non-blocking
    print(job._dashboard_uri())
    # Keep the resource name as the LAST stdout line so the DOE launcher can
    # parse it reliably from the subprocess output.
    print(f"Submitted PipelineJob: {job.resource_name}")


if __name__ == "__main__":
    main()
