# Cause 5: root-cause the steady-state empty-at-200 rate

> **For Claude:** Diagnostic plan. Experiments first, mitigation in a **separate**
> plan (justified at the end). Gate any commit on
> `uv sync --all-groups && uv run ruff format --check && uv run ruff check && uv run ty check src/ && uv run pytest`.
> **NO `Co-Authored-By` / "Generated with Claude Code"** trailers. Git identity:
> Jordan Totten `<jordantotten@google.com>`. Do NOT touch `notebooks/jt_eval_jw.ipynb`.
> Do NOT commit `eval_output/`. NEVER repoint `.env`. Engines updated **in place
> only** — never recreated. Commit/push and open PRs only when explicitly asked.
> **Copy this file to `docs/plans/2026-08-21-cause-5-root-cause.md` and commit it first.**

**Goal:** Explain the ~14% surviving / ~100% attempted empty-at-200 rate on the
coordinator, to the point where we know which lever moves it.

**Architecture:** Four cheap experiments, ordered by cost, each with a pre-registered
decision gate. Three reuse `src/eval/sweep_empty_rate.py` unchanged and only change a
deploy-time env var; one is free log analysis. No mitigation is built until an
experiment names the cause.

**Tech Stack:** existing sweep harness, Cloud Logging, in-place `deploy_coordinator`.

---

## Context

Cause 5 in [empty-at-200-field-guide.md](../../../home/user/geap/geap-tour/docs/notes/empty-at-200-field-guide.md):
after the four known causes were fixed, the coordinator still drops turns at a rate
that responds to no client-side lever. Established, do not re-litigate:

- **~14% survive as empty; ~100% are attempted.** 9 runs / 441 items: 159/180/136
  empty *attempts* per 147 items, collapsing ~8× to ~20 survivors via retries.
- **Flat across concurrency 1/4/8** (14% / 15% / 12%, overlapping Wilson intervals).
  A *fully serial* run on a warm `min_instances=4` engine still loses 14%.
- **Per-request, not one bad replica** — `empty_indices` are scattered, with only
  occasional short clusters.
- **Not agent/case behaviour** — two runs shared 2 of 13 and 6 empty cases.
- **Not a guardrail block** — `input_guardrail_callback` returns `REJECTION_MESSAGE`
  text, so blocks are non-empty by construction.

### The new evidence that reframes it

Cloud Logging on the probe engine (`4380288848559603712`) shows **continuous,
bursty worker lifecycle churn**:

```
shutdowns per 10-min bucket, 12h:
  09:40  45     11:10  52     13:20  96     13:50  85     14:20  26     14:30  31
```

~400+ in 12h on a `min_instances=4` engine, and **96 in a single 10-minute bucket**.
They are **graceful** — `Shutting down...` → `Waiting for application shutdown.` →
`Waiting for OTEL push...` — not SIGKILLs. That matters: a graceful shutdown leaves
no kill signature, which is exactly why the trace-shape heuristics in the field guide
never fired for this cause.

*Caveat to resolve in E1:* it is not yet established whether one log line is one
**container** or one **worker process** (the router OOM note observed 7 worker PIDs
per container). The absolute number changes meaning; the burstiness does not.

## Hypotheses

**H1 — routine instance recycling terminates in-flight requests.** The runtime cycles
workers on its own schedule; any request in flight when its worker drains returns
HTTP 200 with nothing. *Predicts:* concurrency-independent ✓, scattered ✓, run-level
overdispersion (one recycle hits several concurrent requests) ✓, graceful so no kill
signature ✓. **Fits every observation.**

**H2 — request duration is the real exposure variable.** If H1 is the mechanism, the
probability of being killed scales with how long a request is in flight. The
coordinator's turns are long: ~17s p50, of which Memory Bank preload is 3-5s and
"thinking" is uncapped
([coordinator-latency-attribution.md](../../../home/user/geap/geap-tour/docs/notes/coordinator-latency-attribution.md)).
The router's lite-tier turns are ~3-4s and it showed **0/12**. *This unifies H1 with
the coordinator-vs-router asymmetry, and it is the only hypothesis with a knob we
already own.*

**H3 — server-side Model Armor.** The single clean config difference: the coordinator
passes `get_armored_generate_config(COORDINATOR_MODEL)` which attaches
`model_armor_config` on the regional-Gemini path (`armor/config.py:86-96`); the router
passes bare `with_afc_disabled()` (`router/agents.py:310`). A response-template block
could plausibly surface as no candidates. *Weak evidence so far:* a Model Armor log
sweep found **no** sanitize entries in 6h, and 14% would be a very high flag rate for
a benign corpus. Also note `get_model_armor_config()` has **no off switch**, so
testing it needs a code flag.

**H4 — the empties are thought-only turns.** Uncapped thinking on `gemini-2.5-flash`
could burn the turn on thoughts and emit no text, which our `_is_empty_turn` counts as
empty. *Weakened by:* the router also passes no thinking config, yet shows 0/12. Worth
one free check (E1) rather than an experiment.

## Existing pieces to reuse (do NOT reimplement)

- `src/eval/sweep_empty_rate.py` — the whole harness. E2/E3/E4 are **one env var
  different**; add a `--label` passthrough at most, do not fork it.
- `src/eval/_sdk_patches.py:retry_counters` — already separates attempted from
  surviving empties. The *attempt* rate is the sensitive signal; the survivor rate is
  damped ~8× by retries.
- `src/doe/deploy_coordinator.py --update <id>` — in-place probe redeploy, the only
  sanctioned way to change a served env var.
- `src/eval/latency_probe.py` — already buckets coordinator latency by phase; use it
  to measure the duration change in E2, don't re-time by hand.

---

## E1 — Free: is the empty a recycle, and what is in it? (do this first)

No engine calls, no deploy.

1. **Resolve the container-vs-worker ambiguity.** Group the lifecycle lines by their
   `[pid]` prefix and by `service.instance.id` if the log labels carry it. One
   container cycling with 7 workers looks very different from 7 containers cycling.
2. **Correlate.** The sweep recorded `empty_indices` per run and the runs are
   timestamped; compare empty timestamps against `Shutting down...` timestamps. A
   real H1 shows empties clustering in the seconds around a drain.
3. **Look inside an empty turn.** Pull `agent_data` for a known-empty item (the
   pattern is in the trajectory work: `get_evaluation_run` → `get_evaluation_set` →
   `get_evaluation_item`) and dump every part key. **Zero events** ⇒ the stream died
   (H1). **Events with thought parts but no text** ⇒ H4. **A `finishReason` of
   `SAFETY`/blocked** ⇒ H3.

**Gate:** if (3) shows thought-only parts, go straight to E2 with
`COORDINATOR_THINKING_BUDGET=0` and skip the rest. If it shows zero events, H1 is
confirmed as the *mechanism* and E2 tests the *exposure*.

## E2 — Cheap: does shortening the request lower the rate? (tests H2, the money experiment)

Two knobs already exist, both currently unset, both documented as latency levers:

```bash
# arm A — baseline, already measured: ~14%
# arm B — cap thinking
COORDINATOR_MODEL=gemini-2.5-flash ENABLE_MEMORY_PRELOAD_CACHE=1 COORDINATOR_THINKING_BUDGET=0 \
  uv run python -m src.doe.deploy_coordinator --update 4380288848559603712 --min-instances 4
uv run python -m src.eval.sweep_empty_rate --agent-id 4380288848559603712 --workers 4 --repeats 3

# arm C — also drop the 3-5s memory preload
COORDINATOR_MODEL=gemini-2.5-flash COORDINATOR_THINKING_BUDGET=0 \
  uv run python -m src.doe.deploy_coordinator --update 4380288848559603712 --min-instances 4
uv run python -m src.eval.sweep_empty_rate --agent-id 4380288848559603712 --workers 4 --repeats 3
```

Record p50 latency per arm with `latency_probe.py` so the result is "rate vs
duration", not "rate vs flag". **Compare the *attempt* rate**, not just survivors —
retries damp the signal ~8×.

**Gate:** a monotonic rate-vs-duration relationship confirms H2 and hands us a
mitigation we already own. Flat ⇒ duration is not the exposure variable; H1 may still
be the mechanism but the lever is elsewhere.

**Restore the probe to `COORDINATOR_THINKING_BUDGET` unset afterwards** unless E2 says
to keep it — this is the demo engine.

## E3 — Cheap: is the router really different? (n=12 is not evidence)

The whole coordinator-vs-router asymmetry rests on **0/12**. Wilson upper bound on
0/12 is ~24% — it does not exclude 14%.

```bash
uv run python -m src.eval.sweep_empty_rate --agent-id 6134089059699523584 --workers 4 --repeats 3
```

`sweep_empty_rate` currently hard-codes `_select_cases("coordinator_agent", …)`; make
the agent selectable so the router runs its own cases. **Gate:** if the router also
sits at ~14%, the asymmetry is imaginary and H3 (armor, coordinator-only) dies.

## E4 — Only if E1-E3 leave H3 alive: an armor off-switch

Requires a code change, which is why it is last. Add `ENABLE_MODEL_ARMOR`
(default **on**, preserving today's behaviour) to `armor/config.py`, so
`get_armored_generate_config` can omit `model_armor_config`. Deploy the probe with it
off, re-run the sweep.

Guard test: the flag defaults on, and the client-side guardrail is untouched by it —
this must not become an accidental way to ship the demo with governance disabled.

## Docs

Extend the **Cause 5** section of
[offline-eval-empty-turns.md](../../../home/user/geap/geap-tour/docs/notes/offline-eval-empty-turns.md)
and the field guide's cause-5 entry with whichever hypothesis survives — including if
none do. `docs/notes/README.md` is at **197 lines** against its own <200 cap.

---

## Verification

```bash
uv sync --all-groups && uv run ruff format --check && uv run ruff check \
  && uv run ty check src/ && uv run pytest -q

uv run python -m src.eval.sweep_empty_rate --agent-id <ID> --dry-run   # plan only
# and confirm the probe's served env after every arm:
#   curl .../reasoningEngines/4380288848559603712 | jq '.spec.deploymentSpec.env'
```

## Success criteria

- A named mechanism with evidence, **or** an explicit "H1-H4 all survive/all fail,
  here is what would separate them next" — a negative result written up as one.
- Every deploy done **in place**; the probe left on its documented config unless an
  experiment justifies otherwise, and the change recorded if so.
- Attempt-rate reported alongside survivor-rate in every arm.
- No monitored-series change; suite green.

## Why the mitigation is a SEPARATE plan

The fix differs completely by outcome, and three of the four are not "fix the bug":

| outcome | mitigation |
| --- | --- |
| H2 (duration) | ship `COORDINATOR_THINKING_BUDGET` as a served default — a real, owned fix with a latency win attached |
| H1 only (recycling) | not fixable client-side: raise retries, gate `demo_readiness` on an empty-rate ceiling, escalate to the platform with the shutdown census |
| H3 (armor) | a governance-vs-reliability trade-off that is **the owner's call**, not an engineering one |
| none | accept and document; the retries already deliver an 8× reduction |

Committing to an implementation now would mean pre-judging which of those we are
doing. Each also has a different blast radius — H2 changes served behaviour for every
demo, H3 changes the security posture — and deserves its own before/after rather than
being smuggled in behind a diagnosis.

## Caveats

- **Cost:** E2/E3 are ~9 inference-only sweeps plus 2-3 in-place redeploys. E1 is free.
- **The probe is the demo engine.** Every arm redeploys it; leave it on a known-good
  config and never recreate it (a recreate mints a new SPIFFE identity needing a fresh
  `roles/agentregistry.viewer` grant).
- **Do not fix while measuring.** Changing `EVAL_EMPTY_RETRIES` mid-experiment
  invalidates every arm.
- **Attempt rate is the sensitive metric.** A mitigation could halve the true failure
  rate and barely move the survivor rate, or vice versa.
- Another session driving traffic at the probe contaminates a sweep — check first.
