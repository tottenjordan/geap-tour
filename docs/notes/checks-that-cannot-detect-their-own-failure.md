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

## How to look for the next one

Four questions that each caught something here:

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
