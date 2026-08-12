# DOE harvest `--wait` path: the unattended-hang root cause & hardening

## Symptom

After a full 9-point screening (`doe-screening-20260812-073603`), all jobs
reached `PIPELINE_STATE_SUCCEEDED`, but the `run_doe --wait` process did not
return for ~22 min afterward and wrote **no** local `results.csv` / `report.md`.
It had to be killed and the results harvested manually with
`harvest(manifest, wait=False)` + `analyze(...)`.

## What it was NOT (verified, not theorized)

Reproduced against the *completed* experiment (all jobs terminal, all
`full_results.json` present in GCS):

- `poll_jobs(manifest)` converges in ~4 s, 0 sleep rounds, every job returns
  the string `"PIPELINE_STATE_SUCCEEDED"` — the state extraction (`.state.name`)
  is **not** broken, and the terminal-state match is correct.
- Full `harvest(manifest, wait=True)` completes end-to-end in ~19 s, all 9 rows
  populated.

So the wait path is **not deterministically broken**. The stall was a
**transient condition during the ~53-min *live* poll** (jobs still RUNNING),
which the old code had no defenses against — a transient `PipelineJob.get`
stall/error, or a lagging state read, on a job that had in fact finished.

## The three real gaps (why a transient became a multi-hour invisible hang)

1. **No GCS ground-truth fall-through.** `poll_jobs` only trusted
   `PipelineJob.get(...).state`. If that call stalled/errored (`except: continue`)
   for a job whose `full_results.json` was already written, the job stayed
   `pending` until the full **2 h** `poll_timeout_s` — even though the artifact
   the harvest needs next was already in GCS.
2. **No heartbeat.** During RUNNING the loop printed nothing; combined with a
   background runner buffering stdout, a working long poll is indistinguishable
   from a hang.
3. **Unbounded GCS download.** `fetch_results` called `blob.download_as_text()`
   with no timeout — a stalled read could hang the harvest forever.

## Fix (`src/doe/harvest.py`, commit `73303e1`)

- `poll_jobs`: after the state check, **fall through to GCS** — if
  `results_exist(gcs_results)` (a job's `full_results.json` is present), mark it
  done regardless of the polled job state. Injectable `results_exist` for tests.
- `poll_jobs`: flushed per-round heartbeat (`… N job(s) pending after Ns: …`);
  timeouts log which jobs were abandoned.
- `fetch_results`: add a `timeout` (default 300 s) to `download_as_text`.

Tests: fall-through on state error, fall-through on non-terminal state, bounded
timeout when neither state nor results, and download-timeout forwarding.

## Why the GCS fall-through is the right primary guard

The results artifact — not the `PipelineJob` state — is what harvest actually
consumes. The report component writes `full_results.json` as its last step, so
its presence is a stronger, API-independent "done" signal. Treating it as ground
truth makes the poll robust to *any* Vertex-API-side stall.

See also [DOE framework](./doe-framework.md) and
[`router_boundaries` was inert](./doe-router-boundaries-inert.md).
