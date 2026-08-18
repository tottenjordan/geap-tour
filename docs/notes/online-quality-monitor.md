# Online quality monitor (`agent_online_eval/*`, continuous live-traffic scores)

**Posture:** continuous evaluation runs **client-side by choice**.
`src/eval/online_monitor.py` samples live coordinator traffic, scores each
response with LLM rubrics, and publishes a continuous
`custom.googleapis.com/agent_online_eval/*` series (`eval_mode=online`) onto the
same dashboard + alert surface as the offline snapshot — a third honest monitored
surface alongside the offline
[`agent_eval/*`](./offline-eval-monitoring-bridge.md) and `agent_router/*` series.

## Why client-side — and what the native path actually was

The native Vertex Online Evaluators returned `INSUFFICIENT_DATA` under a default
deploy because prompt/response content never landed on the `call_llm` spans the
`onlineEvaluator` parses. That was **not** a hard platform strip (corrected
2026-08-15): the managed `AdkApp` `set_up()` forces the ADK span-content gate
closed unless deployed with `AdkApp(enable_tracing=True)`, now wired behind the
opt-in `ENABLE_SPAN_CONTENT_CAPTURE` flag (root cause + fix in
[[online-eval-content-capture-blocked]] and
[online-eval-content-capture.md](./online-eval-content-capture.md)). So the native
path is **unblockable on demand** — this client-side monitor is the shipped
default **by choice** (model-neutral, no privacy-off content capture on the served
engine), not because the native surface is dead.

The load-bearing insight this monitor exploits: the live response **content is
available client-side** off `stream_query` regardless of the span gate. The
traffic generator already captures `full_response`. So an online monitor is
buildable by scoring sampled live `(prompt, response)` pairs captured client-side,
independent of the trace surface entirely.

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

# Score with the DIVERSE MULTI-MODEL JUDGE PANEL (median + inter-rater reliability)
# instead of the single autorater — higher-trust online scores, prints per-rubric
# Krippendorff alpha + mean spread (roadmap P1.4, now wired online). Panel is
# Gemini-only (gemini-2.5-flash + gemini-3.5-flash): the genai generateContent path
# the judge client uses can't reach partner models like Claude, so diversity spans
# Gemini generations, not vendors. Gemini-3.5 is judged on the global endpoint.
uv run python -m src.eval.online_monitor --agent-id <ENGINE_ID> --panel

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
- **Single autorater by default, panel opt-in:** scoring uses a single
  `gemini-2.5-flash` autorater (`--judge-model` overridable), same as the
  standalone judges. Pass `--panel` to score the rubrics with the diverse
  cross-generation Gemini panel (`judge_panel.build_panel`: `gemini-2.5-flash` +
  `gemini-3.5-flash`) — per-item **median** (robust to one contrarian judge) plus
  per-rubric **Krippendorff alpha** + mean spread so a low-agreement panel is
  visible rather than silently averaged. The panel is **Gemini-only**: the genai
  `generateContent` path the judge client uses only reaches Google-published models
  (a Claude/partner judge resolves to `publishers/google` and 404s — verified live
  2026-08-18), so panel diversity spans Gemini **generations**, not vendors.
  Gemini-3.5 is judged on the **global** endpoint (regional 404s;
  `judge_client.resolve_judge_location` mirrors `config.resolve_model`'s family
  split). Panel mode is N× the judge cost (one call per judge per rubric);
  faithfulness (`--faithfulness`) stays single-judge for now.
- **Client-side sampling cost:** every scored interaction is one LLM judge call
  per rubric; `--sample-rate` bounds that cost. Sampling is a fixed stride, so a
  low rate scores a reproducible subset, not a random one.
- **Client-side by choice, not the native surface:** this is a deliberate
  client-side monitor, not the managed online evaluator. The native path is
  unblockable on demand (`ENABLE_SPAN_CONTENT_CAPTURE=1` →
  `AdkApp(enable_tracing=True)`, see
  [online-eval-content-capture.md](./online-eval-content-capture.md)); this surface
  is preferred because it's model-neutral and needs no privacy-off content capture
  on the served engine.

## Interpretation of a real run (2026-08-15, 31-pair bunch)

A full end-to-end run drove the traffic generator's `QUERIES` corpus (28 cases:
travel/expense/routing across low/medium/high complexity) plus 3 adversarial
`INJECTED_QUERIES` through the pinned coordinator `3639024497392091136` via
`stream_query`, captured client-side, then scored the whole bunch with
`online_monitor --from-json` (99 judge calls: 3 rubrics × 31 pairs). **31/31
captured, 0 errors** (every request returned HTTP 200).

**Published online aggregate (`agent_online_eval/*`, 1-5 axis):**

| Metric | Online (this bunch) | Offline snapshot (`agent_eval/*`) | Δ |
|---|---|---|---|
| `helpfulness` | **2.871** — below the 3.0 floor (alerts) | 4.03 | −1.16 |
| `tool_use_accuracy` | 3.258 | 4.893 | −1.64 |
| `policy_compliance` | 3.032 | 4.846 | −1.81 |

**The headline result: telemetry said 100% success; the online monitor said
helpfulness 2.87 (alerting).** The gap is entirely explained by **empty
responses** — requests that stream 0 characters of visible text yet return HTTP
200. Of the 31 captured pairs, **12 (39%) were empty**, concentrated at the two
extremes of complexity:

| Complexity | Empty / total | Why |
|---|---|---|
| low | 6 / 13 | **cold-start** — the first ~7 requests after an idle engine returned 0c, then it warmed up and answered normally |
| medium | 1 / 7 | one warm blip |
| high | 5 / 8 | **multi-step timeouts** — heavy multi-tool prompts stream only tool-calls / time out before emitting a final answer |
| injected (adversarial) | 0 / 3 | all three got a clean, short (96c) refusal: *"I'm sorry, I can't process that request. Please rephrase your question about travel or expenses."* |

An empty response is correctly scored as **low helpfulness** by the judge (there
is nothing helpful in 0 characters), which is exactly what drags the online
helpfulness mean under the 3.0 floor while the offline snapshot — scored on
curated cases that always elicit a substantive answer — reads 4.03. **This gap is
the entire point of the online surface:** the offline batch and raw request
telemetry both miss silent empty-stream degradation because both see "HTTP 200,
success"; only content-level scoring of *real sampled traffic* catches it.

Why the other two metrics also dip but stay above floor: the empties also blunt
`tool_use_accuracy` (a response with no tool output reads as weaker tool use) and
`policy_compliance` (an empty answer neither cites nor violates policy, scoring
mid-band, ~3.0), but both aggregates stay ≥ 3.0 because the substantive and
adversarial responses (clean refusals score high on policy) pull them back up.

**How it reads across all three surfaces** (`verify_monitors --format json`):
- `coordinator_quality` (offline): 3 metrics all 4.0–4.9, `out_of_bounds=0` — a
  healthy periodic snapshot.
- `online_quality` (this run is the latest of 3 points): helpfulness
  `out_of_bounds=1` (this run's 2.871), 1h trend = this bunch's exact values
  (2.871 / 3.032 / 3.258); the 24h averages (3.90 / 3.46 / 3.98) stay above floor
  because earlier warm-only probe runs scored higher.
- `router_efficiency`: unaffected and all green (routing 91.7%, cost savings
  63.9%, classifier latency 3436 ms) — a different agent on a different axis, as
  intended.

**Operator takeaways.**
1. The online monitor is doing its job: it turned an invisible "39% empty at
   HTTP 200" into a **fired helpfulness alert**. Neither offline eval nor uptime
   telemetry would have surfaced it.
2. The two empty-response causes are **operational, not model-quality**:
   cold-start (mitigate with `--min-instances`/a warmer, or exclude the warm-up
   window from sampling) and high-complexity multi-step timeouts (a latency/step
   budget issue). A production monitor sampling *steady* warm traffic (rather than
   a cold burst of 31) would read materially higher.
3. Empty ≠ error in telemetry — so **content-level online scoring is the only
   layer that sees it**. Keep helpfulness on the alert path.

Related: [[online-eval-content-capture-blocked]] (memory),
[online-eval-content-capture.md](./online-eval-content-capture.md),
[offline-eval-monitoring-bridge.md](./offline-eval-monitoring-bridge.md),
[coordinator-tool-use-quality.md](./coordinator-tool-use-quality.md).
