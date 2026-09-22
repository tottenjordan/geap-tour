"""Failure cluster analysis — group failure patterns from evaluation results.

Runs a quick evaluation against the deployed agent, then analyzes the
results with generate_loss_clusters() to identify systemic failure patterns.

Usage:
    uv run python -m src.eval.failure_clusters <agent-engine-id>
    uv run python -m src.eval.failure_clusters <AGENT_ENGINE_ID>
"""

import sys
import time

import vertexai
from agentplatform import Client, types

from src.config import GCP_PROJECT_ID, GCP_REGION, GCP_STAGING_BUCKET

# Demo prompts used for the quick failure-cluster evaluation pass.
QUICK_EVAL_CASES = [
    "Find me a hotel in Miami",
    "Search for hotels in New York under $350",
    "Check if a $50 meal expense is within policy",
    "Check policy for a $500 entertainment expense",
    "Submit a $45 meals expense for lunch meeting, user ID EMP001",
]

EVAL_METRICS = [
    types.RubricMetric.FINAL_RESPONSE_QUALITY,
    types.RubricMetric.SAFETY,
]


def _resolve_agent_resource_name(agent_id: str) -> str:
    if agent_id.startswith("projects/"):
        return agent_id
    return f"projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/reasoningEngines/{agent_id}"


def _metric_label(metric) -> str:
    """A human-readable name for a metric, for printing.

    ``.name`` FIRST: the SDK hands back ``LazyLoadedPrebuiltMetric``, which has
    ``.name`` but no ``.value``, so a ``.value``-first lookup fell through to
    ``str(metric)`` and printed ``<...LazyLoadedPrebuiltMetric object at 0x7f...>``
    as a section header — in a demo cell, in front of an audience.

    Falls back rather than raising: an SDK that renames the attribute should degrade
    to an ugly label, not crash the cell mid-presentation.
    """
    return str(getattr(metric, "name", None) or getattr(metric, "value", None) or metric)


def analyze_failure_clusters(agent_id: str):
    """Run evaluation and analyze failure clusters."""
    agent_resource = _resolve_agent_resource_name(agent_id)

    vertexai.init(
        project=GCP_PROJECT_ID,
        location=GCP_REGION,
        staging_bucket=f"gs://{GCP_STAGING_BUCKET}",
    )
    client = Client(project=GCP_PROJECT_ID, location=GCP_REGION)

    import pandas as pd

    rows = [
        {
            "prompt": case,
            "session_inputs": types.evals.SessionInput(user_id="cluster-analysis-user"),
        }
        for case in QUICK_EVAL_CASES
    ]
    eval_df = pd.DataFrame(rows)

    print(f"[1/3] Running inference against {agent_resource}...")
    t0 = time.time()
    inference_result = client.evals.run_inference(agent=agent_resource, src=eval_df)
    print(f"  Inference done in {time.time() - t0:.1f}s")

    print("[2/3] Evaluating with metrics...")
    eval_result = client.evals.evaluate(
        dataset=inference_result,
        metrics=EVAL_METRICS,
    )
    print("  Evaluation complete")

    print("[3/3] Analyzing failure clusters...")
    for metric in EVAL_METRICS:
        metric_name = _metric_label(metric)
        print(f"\n--- Clusters for {metric_name} ---")
        try:
            clusters = client.evals.generate_loss_clusters(
                eval_result=eval_result,
                metric=metric,
            )
            if not clusters:
                print("  No failure clusters found (all cases passed)")
                continue
            for i, cluster in enumerate(clusters, 1):
                title = getattr(cluster, "title", "Untitled")
                description = getattr(cluster, "description", "")
                count = getattr(cluster, "sample_count", 0)
                score = getattr(cluster, "avg_score", None)
                print(f"  Cluster {i}: {title}")
                print(f"    Description: {description}")
                print(f"    Samples: {count}")
                if score is not None:
                    print(f"    Avg score: {score:.2f}")
        except Exception as e:
            print(f"  Error: {e}")

    return eval_result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m src.eval.failure_clusters <agent-engine-id>")
        sys.exit(1)
    analyze_failure_clusters(sys.argv[1])
