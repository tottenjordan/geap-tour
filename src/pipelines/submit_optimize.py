"""Compile and submit the GEAP optimize pipeline to Vertex AI Managed Pipelines.

Runs GEPA prompt optimization on Vertex (not locally). Manual trigger only.

Examples::

    # Optimize the coordinator prompt (default target + sampler config)
    uv run python -m src.pipelines.submit_optimize --experiment-id opt-20260807

    # Optimize under a specific base model (baked as env, like the DOE fan-out)
    COORDINATOR_MODEL=gemini-3.1-pro-preview \
        uv run python -m src.pipelines.submit_optimize --agent-tag coordinator-pro

The optimized prompt lands at
``gs://{bucket}/optimize-results/{experiment_id}/{agent_tag}/`` — paste it back
into the agent as ``INSTRUCTION_GEPA`` and redeploy (human-in-the-loop).
"""

import argparse

from google.cloud import aiplatform
from kfp import compiler

from src.config import GCP_PROJECT_ID, GCP_REGION, GCP_STAGING_BUCKET
from src.pipelines.optimize_pipeline import optimize_pipeline

# Reuse the project compute SA (same as the eval pipeline; least-privilege
# scope-down is a tracked follow-up).
DEFAULT_SERVICE_ACCOUNT = "934903580331-compute@developer.gserviceaccount.com"
PIPELINE_SPEC = "optimize_pipeline.yaml"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-opt-module", default="src/agents/coordinator")
    parser.add_argument("--sampler-config", default="src/optimize/sampler_config.json")
    parser.add_argument("--optimizer-config", default="")
    parser.add_argument("--experiment-id", default="")
    parser.add_argument("--agent-tag", default="coordinator")
    parser.add_argument("--spec-path", default=PIPELINE_SPEC)
    parser.add_argument("--service-account", default=DEFAULT_SERVICE_ACCOUNT)
    args = parser.parse_args()

    compiler.Compiler().compile(optimize_pipeline, args.spec_path)

    aiplatform.init(
        project=GCP_PROJECT_ID,
        location=GCP_REGION,
        staging_bucket=f"gs://{GCP_STAGING_BUCKET}",
    )
    job = aiplatform.PipelineJob(
        display_name="geap-optimize",
        template_path=args.spec_path,
        pipeline_root=f"gs://{GCP_STAGING_BUCKET}/pipeline-root",
        parameter_values={
            "agent_opt_module": args.agent_opt_module,
            "sampler_config_path": args.sampler_config,
            "optimizer_config_path": args.optimizer_config,
            "experiment_id": args.experiment_id,
            "agent_tag": args.agent_tag,
        },
    )
    job.submit(service_account=args.service_account)  # non-blocking
    print(job._dashboard_uri())
    # Keep the resource name as the LAST stdout line (parseable by a launcher).
    print(f"Submitted PipelineJob: {job.resource_name}")


if __name__ == "__main__":
    main()
