# Online quality monitor (`agent_online_eval/*`, continuous live-traffic scores)

**Posture:** the native Vertex Online Evaluators are platform-blocked for our
agents, so continuous evaluation runs **client-side**. `src/eval/online_monitor.py`
samples live coordinator traffic, scores each response with LLM rubrics, and
publishes a continuous `custom.googleapis.com/agent_online_eval/*` series
(`eval_mode=online`) onto the same dashboard + alert surface as the offline
snapshot — a third honest monitored surface alongside the offline
[`agent_eval/*`](./offline-eval-monitoring-bridge.md) and `agent_router/*` series.

## Why native online eval stays dead — and why *this* works anyway

The managed Agent Engine runtime strips prompt/response content from the ADK
trace surface the native `onlineEvaluator` parses, so every native cycle returns
`INSUFFICIENT_DATA` (root cause in [[online-eval-content-capture-blocked]] and
[offline-eval-monitoring-bridge.md](./offline-eval-monitoring-bridge.md)). **No
lever from our side unblocks the trace surface.**

The load-bearing insight: only the *trace* surface is stripped — the live
response **content is available client-side** off `stream_query`. The traffic
generator already captures `full_response`. So an online monitor is buildable by
scoring sampled live `(prompt, response)` pairs captured client-side, entirely
sidestepping the trace stripping.

## Two surfaces, never blurred

| Surface | Family | Cadence | Label | Source |
|---|---|---|---|---|
| Offline snapshot | `agent_eval/*` | one write per eval run | `eval_mode=offline` | `publish_offline_eval` (batch eval of the deployed engine) |
| **Online continuous** | `agent_online_eval/*` | continuous sampled | `eval_mode=online` | `online_monitor` (scores sampled live `stream_query` traffic) |

Both ride the **same 1-5 rubric axis and the same 3.0 alert floor** (a 0-1 score
is scaled `round(score * 5.0, 3)` before publish), so they chart together and are
directly comparable — but the separate metric family + `eval_mode` label keep the
continuous online signal distinct from the periodic offline snapshot.

## What it scores (no rubric drift)

Three metrics, matching `quality_alerts.ONLINE_MONITORED_METRICS`:

| Metric | Rubric builder | Shared with offline bridge? |
|---|---|---|
| `helpfulness` | `build_helpfulness_prompt` (online-only) | No — the offline helpfulness comes from the SDK `FINAL_RESPONSE_QUALITY` metric, which can't be called per-interaction, so the online path scores it directly with the same `Score: <1-5>` contract. |
| `tool_use_accuracy` | `tool_use_judge.build_tool_use_prompt` | Yes — the EXACT delegation-aware `geap_tool_use` rubric (does not penalize `transfer_to_agent` routing; see [coordinator-tool-use-quality.md](./coordinator-tool-use-quality.md)). |
| `policy_compliance` | `policy_judge.build_policy_prompt` | Yes — the EXACT standalone `policy_judge` rubric. |

Reusing the standalone-judge rubric builders means the online and offline
surfaces score the same behavior with the same yardstick — no drift.

## Architecture (pure core + thin live driver)

The pure, unit-tested core (no GCP, injected `generate_fn` + fake metric client):

- `parse_score(text)` — last `Score: N` (1-5) → 0-1; `None` when unparseable
  (dropped, not zeroed). Mirrors the standalone judges' parsers.
- `score_interaction(prompt, response, generate_fn, metrics=None)` — one
  interaction → `{metric: 0-1}`, skipping unparseable verdicts.
- `sample_interactions(items, sample_rate)` — deterministic stride
  (`round(1/rate)`), reproducible/testable rather than RNG. `>=1` all, `<=0` none.
- `aggregate_scores(...)` — mean per metric over the interactions that produced it.
- `publish_online_scores(scores, ...)` — scales 0-1 → 1-5, filters to the
  monitored names (no drift), tags `eval_mode=online`, writes via
  `metrics.write_online_quality_scores`.
- `score_and_publish(pairs, ...)` — the shared core of both CLI paths
  (sample → score → aggregate → publish); `dry_run` still computes/returns scores.

The thin live driver is *not* unit-tested (like the standalone judges' `run_*_eval`):

- `capture_live_interactions(agent, prompts)` — drives `stream_query`, reusing the
  traffic generator's `_extract_text` so captured text matches the traffic tooling.
- `run_online_monitor(...)` — resolves the engine, captures, scores, publishes;
  `agent`/`generate_fn`/`writer` are injectable for offline wiring tests.

## How to run it

```bash
# Sample live coordinator traffic → agent_online_eval/* (default AGENT_ENGINE_ID)
uv run python -m src.eval.online_monitor --agent-id <ENGINE_ID>

# Cap probe prompts + sample a fraction of them (LLM-judge cost control)
uv run python -m src.eval.online_monitor --agent-id <ENGINE_ID> --samples 6 --sample-rate 0.5

# Score externally-captured pairs instead of driving live traffic
uv run python -m src.eval.online_monitor --from-json sampled_traffic.json
#   accepts [{"prompt": ..., "response": ...}, ...] or [[prompt, response], ...]

# Preview without writing to Cloud Monitoring
uv run python -m src.eval.online_monitor --agent-id <ENGINE_ID> --dry-run

# Stamp a series label (e.g. per-model, for a bake-off)
uv run python -m src.eval.online_monitor --agent-id <ENGINE_ID> --label model=gemini-3.6-flash

# Read it back — third block alongside coordinator_quality + router_efficiency
uv run python -m src.eval.verify_monitors --format json
```

The `agent_online_eval/*` descriptors must be materialized before the alert
policies can reference them; the demo runbook's monitoring step seeds them and
`quality_alerts all` creates the online alert family. Dashboard "Online Eval: *"
widgets (aggregate + per-`model` breakdown) chart the series live.

## Honest caveats

- **Self-driven probe traffic by default:** the live CLI drives a small
  cross-domain probe set (`ONLINE_PROBE_PROMPTS`) so a demo run exercises all
  three rubrics. In production you'd feed real sampled traffic via `--from-json`.
- **Gemini-only judge:** scoring uses a single `gemini-2.5-flash` autorater
  (`--judge-model` overridable), same as the standalone judges — not a panel.
- **Client-side sampling cost:** every scored interaction is one LLM judge call
  per rubric; `--sample-rate` bounds that cost. Sampling is a fixed stride, so a
  low rate scores a reproducible subset, not a random one.
- **Still not the native surface:** this is a client-side workaround, not the
  managed online evaluator. It proves continuous eval for the demo despite the
  platform block; it does not un-block the native path.

Related: [[online-eval-content-capture-blocked]] (memory),
[offline-eval-monitoring-bridge.md](./offline-eval-monitoring-bridge.md),
[coordinator-tool-use-quality.md](./coordinator-tool-use-quality.md).
