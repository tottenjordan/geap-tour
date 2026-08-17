"""Harvest DOE results: poll each design point's job, pull its full_results.json
from GCS, and assemble a tidy pandas DataFrame (one row per design point).

Response variables (exact JSON paths in full_results.json):
  - batch metrics: results["batch"]["agents"][AGENT]["metrics"][KEY]["score"]
    where KEY looks like "agent_engine_0/<metric>_v<N>". The API assigns the
    "agent_engine_0/" prefix and the "_vN" version suffix, so we match on the
    metric *base name* (version-stripped) rather than an exact key.
  - complexity: results["complexity"]["accuracy"]["accuracy"] and
    results["complexity"]["cost_efficiency"][{savings_pct,routed_cost_usd,
    all_opus_cost_usd}].
  - simulated: results["simulated"][AGENT]["passed"] (bool).

Anything missing or malformed yields NaN rather than raising, so one bad run
does not sink the whole harvest.
"""

from __future__ import annotations

import json
import re
import time

import pandas as pd

from src.config import GCP_STAGING_BUCKET

DEFAULT_AGENT = "coordinator_agent"

# Canonical batch metric base names (version-stripped).
BATCH_METRICS = (
    "final_response_quality",
    "hallucination",
    "safety",
    "tool_use_quality",
    "instruction_following",
    "final_response_match",
)

_VERSION_SUFFIX = re.compile(r"_v\d+$")
_TERMINAL_STATES = {
    "PIPELINE_STATE_SUCCEEDED",
    "PIPELINE_STATE_FAILED",
    "PIPELINE_STATE_CANCELLED",
}


def _metric_base(key: str) -> str:
    """agent_engine_0/tool_use_quality_v1 -> tool_use_quality."""
    return _VERSION_SUFFIX.sub("", key.rsplit("/", 1)[-1])


def _safe_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def parse_batch_metrics(results: dict, agent: str) -> dict[str, float]:
    """Extract the 6 batch metric scores for an agent (missing -> NaN)."""
    out = {m: float("nan") for m in BATCH_METRICS}
    try:
        metrics = results["batch"]["agents"][agent]["metrics"]
    except (KeyError, TypeError):
        return out
    if not isinstance(metrics, dict):
        return out
    by_base = {_metric_base(k): v for k, v in metrics.items()}
    for m in BATCH_METRICS:
        detail = by_base.get(m)
        if isinstance(detail, dict):
            out[m] = _safe_float(detail.get("score"))
    return out


def parse_complexity(results: dict) -> dict[str, float]:
    """Extract routing accuracy + cost-efficiency (missing -> NaN)."""
    comp = results.get("complexity") or {}
    accuracy = comp.get("accuracy") or {}
    cost = comp.get("cost_efficiency") or {}
    return {
        "routing_accuracy": _safe_float(accuracy.get("accuracy")),
        "savings_pct": _safe_float(cost.get("savings_pct")),
        "routed_cost_usd": _safe_float(cost.get("routed_cost_usd")),
        "all_opus_cost_usd": _safe_float(cost.get("all_opus_cost_usd")),
    }


def parse_simulated(results: dict, agent: str) -> float:
    """1.0 if the simulated eval passed, 0.0 if failed, NaN if absent."""
    sim = results.get("simulated") or {}
    entry = sim.get(agent)
    if not isinstance(entry, dict) or "passed" not in entry:
        return float("nan")
    return 1.0 if entry.get("passed") else 0.0


def parse_results(results: dict, agent: str = DEFAULT_AGENT) -> dict[str, float]:
    """Flatten one run's full_results.json into response-variable columns."""
    if not isinstance(results, dict):
        results = {}
    row: dict[str, float] = {}
    row.update(parse_batch_metrics(results, agent))
    row.update(parse_complexity(results))
    row["sim_passed"] = parse_simulated(results, agent)
    return row


# --- GCS + polling (injectable for tests) -----------------------------------


def fetch_results(gcs_uri: str, *, client=None, timeout: float = 300) -> dict:
    """Download and parse a full_results.json from a gs:// URI (malformed -> {}).

    ``timeout`` bounds the GCS read so a stalled download can't hang the harvest
    indefinitely (the download leg had no timeout, a latent forever-hang).
    """
    try:
        from google.cloud import storage

        client = client or storage.Client()
        assert gcs_uri.startswith("gs://")
        bucket_name, _, blob_path = gcs_uri[len("gs://") :].partition("/")
        blob = client.bucket(bucket_name).blob(blob_path)
        return json.loads(blob.download_as_text(timeout=timeout))
    except Exception as e:
        print(f"fetch_results failed for {gcs_uri}: {e}")
        return {}


def _get_job_state(resource_name: str) -> str:
    from google.cloud import aiplatform

    return aiplatform.PipelineJob.get(resource_name).state.name


def _results_exist(gcs_uri: str, *, client=None) -> bool:
    """True if a run's full_results.json is already present in GCS.

    This is the ground truth the harvest actually needs (the artifact it will
    download next), so it lets the poll infer completion even when the
    PipelineJob API stalls or errors on a job that has in fact finished.
    """
    if not gcs_uri:
        return False
    try:
        from google.cloud import storage

        client = client or storage.Client()
        assert gcs_uri.startswith("gs://")
        bucket_name, _, blob_path = gcs_uri[len("gs://") :].partition("/")
        return client.bucket(bucket_name).blob(blob_path).exists()
    except Exception:
        return False


def poll_jobs(
    manifest: dict,
    *,
    interval_s: int = 30,
    timeout_s: int = 3600,
    get_state=_get_job_state,
    sleep=time.sleep,
    results_exist=_results_exist,
) -> dict[str, str]:
    """Block until every submitted job reaches a terminal state (or timeout).

    Two robustness guards over a naive state poll (both defend against the
    observed unattended hang, where jobs finished but the wait never returned):

      * **GCS fall-through.** If ``get_state`` stalls, errors, or lags behind on
        a job whose ``full_results.json`` is already present, the job is treated
        as done — the results artifact is what the next step consumes anyway.
      * **Visible heartbeat.** Each round prints (flushed) how many jobs remain,
        so a working long poll is distinguishable from a hang even when stdout
        is buffered by a background runner.
    """
    pending = {
        e["design_point"]: {
            "resource": e["job_resource"],
            "gcs_results": e.get("gcs_results", ""),
        }
        for e in manifest["points"]
        if e.get("job_resource")
    }
    states: dict[str, str] = {}
    waited = 0
    while pending and waited <= timeout_s:
        for dp, job in list(pending.items()):
            state = None
            try:
                state = get_state(job["resource"])
            except Exception as e:
                print(f"poll {dp}: {e}", flush=True)
            if state in _TERMINAL_STATES:
                states[dp] = state
                del pending[dp]
                print(f"  {dp}: {state}", flush=True)
                continue
            # Ground truth: results already written => done, whatever the API says.
            if results_exist(job["gcs_results"]):
                states[dp] = "PIPELINE_STATE_SUCCEEDED"
                del pending[dp]
                print(f"  {dp}: results present (job state={state})", flush=True)
        if pending:
            print(
                f"  … {len(pending)} job(s) pending after {waited}s: {', '.join(sorted(pending))}",
                flush=True,
            )
            sleep(interval_s)
            waited += interval_s
    for dp in pending:
        states[dp] = "PIPELINE_STATE_TIMEOUT"
        print(f"  {dp}: PIPELINE_STATE_TIMEOUT (gave up after {waited}s)", flush=True)
    return states


def build_dataframe(
    manifest: dict,
    results_by_point: dict[str, dict],
    agent: str = DEFAULT_AGENT,
) -> pd.DataFrame:
    """One row per design point: factor columns + response columns."""
    factor_names = manifest.get("factors", [])
    rows = []
    for entry in manifest["points"]:
        dp = entry["design_point"]
        row = {"design_point": dp, "is_baseline": entry.get("is_baseline", False)}
        for fname in factor_names:
            row[fname] = entry.get("assignments", {}).get(fname)
        row.update(parse_results(results_by_point.get(dp, {}), agent))
        rows.append(row)
    return pd.DataFrame(rows)


def harvest(
    manifest: dict,
    *,
    agent: str = DEFAULT_AGENT,
    out_dir: str = ".",
    wait: bool = True,
    poll_timeout_s: int = 7200,
    poll_interval_s: int = 30,
    fetch=fetch_results,
    poll=poll_jobs,
) -> pd.DataFrame:
    """Poll (optional), download each run's results, build + persist the table.

    ``poll_timeout_s`` defaults to 2h: a ``thorough``-fidelity point can take
    just over an hour, so the old 1h default could cut off before a slow run's
    report landed. Raise it further for large full-factorial fan-outs.
    """
    if wait:
        poll(manifest, timeout_s=poll_timeout_s, interval_s=poll_interval_s)
    results_by_point = {e["design_point"]: fetch(e["gcs_results"]) for e in manifest["points"]}
    df = build_dataframe(manifest, results_by_point, agent)

    import os

    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "results.csv")
    df.to_csv(csv_path, index=False)

    prefix = f"eval-results/doe/{manifest['experiment_id']}"
    try:
        from google.cloud import storage

        storage.Client().bucket(GCP_STAGING_BUCKET).blob(
            f"{prefix}/results.csv"
        ).upload_from_filename(csv_path)
        print(f"results table → gs://{GCP_STAGING_BUCKET}/{prefix}/results.csv")
    except Exception as e:
        print(f"results CSV upload skipped: {e}")
    return df
