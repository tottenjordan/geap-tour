# Checks that cannot detect their own failure

**Date:** 2026-09-07 · A deliberate sweep, prompted by hitting the same shape five
times in one session.

## The pattern

A check whose **broken state is indistinguishable from its healthy state**. It
does not throw, it does not go red — it reports "nothing found", which is exactly
what it reports when everything is fine. The cost is worse than having no check,
because a check that reads green buys false confidence.

Every instance below was found by accident, days or weeks after it broke. That is
the tell: if the only way you learn a check is broken is by tripping over the
thing it was supposed to catch, the check has no self-awareness.

## The five that prompted the sweep

| # | Check | How it read | Actually |
| --- | --- | --- | --- |
| 1 | ADC token via `google.auth.default()` | engines `UNREACHABLE` | empty `scopes` → IAM 400 under WIF impersonation; worked on every dev machine |
| 2 | AST guard for #1 | suite green | matched only the attribute form; `from google.auth import default` walked past |
| 3 | Engine config tally | `0 critical, 0 advisory` | engine was never inspected — an unreachable engine has no findings |
| 4 | Actions step status | `conclusion: success` | `continue-on-error` rewrites it; only `outcome` is the truth |
| 5 | `agent_eval/tool_faithfulness` | alert configured | nothing published it for days (#84) |

All five are fixed and pinned by tests — see the table at the bottom.

## What the sweep then found

Searching for the *shape* rather than the instances turned up three more.

**A second unpublished-but-alerted series.** `agent_online_eval/tool_faithfulness`
is in `ONLINE_MONITORED_METRICS` (alert `< 3.0`) and had **zero points** while its
three siblings had n=12 — the cron's online step runs without `--faithfulness`.
Identical to #5, on the online twin, and it had been sitting there since the online
family was created. Fixed with its own bounded cron step (`--samples 6`), mirroring
what #84 did offline, because this is the priciest judge here.

**`verify_monitors` could not report that state.** This is the important one. A
metric with no points simply never appeared in `metrics`, so the surface rendered
healthy. The tool whose entire job is summarising monitoring coverage was blind to
the most dangerous coverage failure there is. It now emits `missing` per surface and
a top-level `unpublished` list, and the workflow raises a `::warning::` per entry —
so the *next* instance is found by tooling rather than by luck.

**An orphaned alert policy.** `complexity_routing_accuracy` had an **enabled**
policy on a descriptor nothing writes (its only code reference is an eval rubric
name, which `publish_eval_metrics` filters out). Deleted. Same class as the
`routing_accuracy_pct` policy orphaned by the rename, which was also deleted.

### A fourth instance: the engine nobody was comparing against anything

`sonnet_agent` `8467456143491334144` — a deployment of this repo, abandoned
2026-05-21, deleted 2026-09-08. Zero traffic for 30 days, on the 4Gi default that
OOM-kills workers, superseded by `sonnet_agent_jt1`. It survived **~3.5 months**
because nothing compared *what is deployed* against *what is referenced*, and it was
found by accident while chasing an unrelated doc reference.

The instructive part is the design trap. The obvious detector — reconcile
*labelled-ours* against *config-referenced* — **would have missed it**, because it
had no label. It predated `RESOURCE_LABELS`. A detector that cannot catch its own
founding case is this note's thesis applied to the fix rather than the bug.

So `src/deploy/find_orphan_engines.py` decides ownership by an **env fingerprint**
(the engine carries our Agent Registry MCP resource names), which a deployment of
this repo cannot lack — `src/registry.py` needs them to resolve any tool. Measured
against the live project before committing to it:

| signal | engines |
| --- | --- |
| labelled **and** fingerprint | 8 — the fleet, both signals agreeing |
| labelled only | 0 |
| **fingerprint only** | **1** — what a label-only detector misses |
| neither | 30 — other teams', correctly excluded |

Zero false positives across 30 foreign engines. Three properties were non-obvious:

* **It lists a shared project**, which `verify_engine_config.default_targets`
  deliberately refuses to do ("listing would invite reporting on — or worse, acting
  on — engines that are not ours"). The exception is earned rather than ignored: no
  delete path, and it **never names an engine that fails the fingerprint** — foreign
  ones appear only in a count, asserted by a test.
* **A `KNOWN_UNREFERENCED` allowlist**, because the first live run flagged the demo
  probe, which is deliberately kept and deliberately absent from `.env`. A check that
  fires on known-good state every run is one people learn to skim. Entries are
  *displayed* with their reason rather than filtered away — a suppression you cannot
  see is indistinguishable from a bug.
* **The detector detects its own incompleteness.** `ENGINE_ID_VARS` is
  hand-maintained, and a new engine-id variable added elsewhere would turn a *live*
  engine into a reported orphan. A test walks `src/` with `ast` and fails if the list
  does not cover every `*_ENGINE_ID` / `*_AGENT_ID` read.

## What was checked and found sound

Recording these so the next sweep does not re-litigate them:

* **`engine_exists` swallowing every exception** — deliberate and documented. It
  fails *closed* ("unusable" → stop), and distinguishing gone from unreachable
  would not change a preflight's decision.
* **`verify_memory` always exiting 0** — it is an inspection CLI, not a gate. The
  actual gate is `demo_readiness`, which checks `len(facts) > 0` and is marked
  critical. The name is a little misleading; the behaviour is not wrong.
* **`eval_gate.yaml`'s engine-config step** — its `outcome` is absent from the
  summary table, but the *content* is surfaced by a dedicated section that handles
  both the UNREACHABLE and no-output cases. Minor inconsistency, not a blind spot.

### The purest instance yet: a gate that had never run (2026-09-08)

`.github/workflows/eval_gate.yaml` — the advisory coordinator quality gate, including
the multi-turn and empty-at-200 smoke steps added for roadmap P2.9 — had **never
executed once**. All 15 invocations since it shipped on 2026-08-14 were `skipped`: it
is gated on a `run-eval` label nobody ever applied.

This is the thesis in its cleanest form. The earlier instances returned a *wrong*
answer. This one returned **no answer, indefinitely**, and its run list looked
perfectly orderly while doing so — fifteen tidy rows, every one of them nothing. The
engine-config step that later caught a 9-day-stale CI variable lives in this workflow
and had never run either; that finding came from a manual invocation.

Every CLI flag the workflow passes was verified to still exist, so it was *plausibly*
correct — which is precisely the state that needs proving rather than assuming. The
fix is not a better check, it is a **cadence**: a weekly schedule alongside the label
gate, so "has never run" becomes "runs, and we would see it break".

### The guard that covered half its surface, again (2026-09-08)

`tests/test_no_hardcoded_values.py` scanned shell scripts for the project id and
number, but its 19-digit **engine-id** check ran only over `*.py`. So the sweep that
existed to delete stale identifiers reported clean while
`scripts/setup_governance_policies.sh` still held two `:-<19 digits>` fallbacks
pointing at **deleted** engines.

The bug behind them was worse than the literals:

```sh
AGENT_ENGINE_ID="${COORDINATOR_AGENT_ID:-${AGENT_ENGINE_ID:-<literal>}}"
ROUTER_ENGINE_ID="${ROUTER_ENGINE_ID:-${AGENT_ENGINE_ID:-<literal>}}"
```

The second line reads `AGENT_ENGINE_ID` *after* the first overwrote it, so an unset
`ROUTER_ENGINE_ID` resolved the router **to the coordinator**. `grant_registry_read`
then granted `roles/agentregistry.viewer` to the coordinator twice and to the router
never — the documented remediation for the router's 403 MCP-resolution fallback,
silently not applied — while printing `Router … ok`. Running the pre-fix script proves
it: both ids resolve to `2479350891879071744`, an engine that no longer exists.

Note this is the *same lesson* `_py_files()` already carries for
`scripts/generate_pptx.py`, one file type over. A guard's coverage is itself a thing
that can be wrong, and it fails in the quiet direction.

**And the loader had inverted its own documented contract.** `scripts/lib/config.sh`
did `set -a; source .env`, which runs every assignment unconditionally — so `.env`
*overwrote* variables the caller had explicitly set, while the comment directly above
claimed the opposite ("NOT over an already-exported variable"). `GCP_PROJECT_ID=my-sandbox
bash scripts/setup_governance_policies.sh` granted IAM in `hybrid-vertex`. It also
disagreed with `src/config.py`, whose `load_dotenv()` defaults to `override=False`:
one variable, two answers, depending on which language asked.

## How to look for the next one

Six questions that each caught something here:

1. **Can this check distinguish "found nothing" from "did not run"?** If not, make
   the second state explicit — `missing`, `UNREACHABLE (not checked)`,
   `insufficient_history`, `INCONCLUSIVE`. This repo now has four such states, and
   every one exists because the two were once conflated.
2. **Is the detector itself tested, or only its output on known-good input?** A
   test asserting `assert not offenders` passes when the finder is broken. #2 is
   exactly this. Feed the detector something it *must* catch.
3. **Does every alerted metric have a scheduled writer?** Not "a writer" — #5 had
   a writer nobody ran. `verify_monitors --format json | jq .unpublished` answers
   this now.
4. **Does every live alert policy correspond to a declared metric?** Renames and
   deletions leave policies behind, watching nothing, enabled.
5. **Does every deployed artifact trace back to something that references it?** Not
   "is it labelled ours" — the thing you are hunting is precisely the one that
   predates whatever convention you would filter on. Pick an identifier the artifact
   cannot be missing.
6. **Has this check ever actually run — and how would you know?** A `skipped` run is
   not a passing run, and nothing distinguishes fifteen of them from a healthy
   history. Check the *execution* record, not the code: `gh run list --workflow X`
   with every row `skipped` is a check that does not exist. Anything gated on a human
   remembering a label will eventually never run; give it a cadence.
7. **Does the comment describe what the code does?** Twice now a comment has asserted
   the correct behaviour directly above code doing the opposite (`config.sh`'s
   override contract; the `default_targets` docstring). Prose is not tested, so it
   drifts silently and then actively misleads the next reader into not checking.

## Where each is pinned

| Instance | Guard |
| --- | --- |
| empty-scope token | `tests/test_auth.py::TestScopesAreAlwaysRequested` |
| AST guard itself | `tests/test_auth.py::TestTheGuardItself` (6 cases: 3 call spellings, scoped call, prose, same-named unrelated call) |
| uninspected-engine tally | `tests/test_engine_baseline.py::TestUnreachableEnginesAreNotReportedAsHealthy` |
| `conclusion` vs `outcome` | `tests/test_monitoring_publish.py` — every `continue-on-error` step's `outcome` must reach the summary and the fail-guard |
| offline faithfulness published | `tests/test_monitoring_publish.py::TestFaithfulnessIsActuallyPublished` |
| online faithfulness published | `tests/test_monitoring_publish.py::TestOnlineFaithfulnessIsScheduled` |
| alerted-but-unpublished, generally | `tests/test_online_eval.py::TestAlertedButUnpublishedIsReported` |
| orphaned engines | `tests/test_find_orphan_engines.py` (fingerprint-not-label, no foreign ids named, allowlist explains itself, `ENGINE_ID_VARS` completeness) |
| engine ids in shell | `tests/test_no_hardcoded_values.py::TestShellScriptsAreEnvDriven::test_no_shell_script_embeds_an_engine_id`, with two planted-literal cases in `TestGuardsAreNotVacuous` |
| router↔coordinator aliasing | `tests/test_no_hardcoded_values.py::TestTheGovernanceScriptCannotSilentlyPickTheWrongEngine` — drives the real script offline; distinct error messages prove which check was reached |
| `.env` override contract | `tests/test_no_hardcoded_values.py::TestTheLoaderDoesNotClobberTheEnvironment`, incl. a bash-vs-Python agreement test |
| the gate that never ran | a weekly `schedule` in `eval_gate.yaml` — a cadence, not an assertion; nothing else distinguishes `skipped` from healthy |
