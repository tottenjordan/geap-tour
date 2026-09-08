# CI/CD eval gate (advisory, opt-in)

A demonstrable "quality gate on a PR" — like the pattern `jswortz/geap-tour` shows
(generate scenarios → run inference → rubric autorater → block on `< 3.0`) — built
so it **illustrates the capability without slowing normal development**. Two tiers:

## Tier 1 — deterministic safety (always-on, no cloud, seconds)

`tests/test_eval_gate_safety.py`, run by the existing required `tests.yaml`. A
corpus-driven regression check with **no LLM / no network**:

- every prompt in `ADVERSARIAL_PROMPTS` (injection, role-override, system-prompt
  leak, `<script>`) must be *refused* by `src.armor.config.input_guardrail_callback`;
- every prompt in `BENIGN_PROMPTS` (real travel/expense asks) must *pass* — an
  over-block regression fails the gate too;
- the coordinator must actually wire the guardrail (`before_agent_callback`), or the
  refusal guarantee is vacuous on the deployed engine.

Unit-level guardrail behavior lives in `tests/test_guardrail.py`; this file is the
gate framing (weakening `BLOCKED_PATTERNS` turns one red). It blocks merge only
through the pytest check that already blocks — no new infra, no credentials.

## Tier 2 — rubric eval (opt-in, advisory)

`.github/workflows/eval_gate.yaml`. Reuses `src/eval/multi_agent_batch_eval.py`
as-is (it already does inference + 6 rubrics + threshold + `sys.exit(1)`), with the
new `--limit` flag (`_select_cases`) capping cases so a run is ~3-5 min.

- **Trigger:** `pull_request` (`labeled`/`synchronize`, path-filtered to
  `src/agents/**`, `src/mcp_servers/**`, `src/eval/**`) **only when the PR carries
  the `run-eval` label**, or `workflow_dispatch` (with a `threshold` input).
- **Skip-guard:** the job is gated on `vars.WIF_PROVIDER != ''`, so it no-ops
  cleanly on forks / repos without WIF (never a red failure for missing creds).
- **Report:** writes a per-metric PASS/FAIL table to `$GITHUB_STEP_SUMMARY` (no
  PR-write permission needed; `permissions: contents:read + id-token:write`).
- **Advisory:** intentionally **not** a branch-protection required check — a failing
  score shows a red mark as a signal but does not block merge. Flip to blocking by
  adding it to required checks and/or dropping the label gate.

Required repo config: vars `WIF_PROVIDER`, `WIF_SERVICE_ACCOUNT`, `AGENT_ENGINE_ID`,
plus a `run-eval` label.

## The honest limitation

All rubric scoring needs a **deployed** Agent Engine —
`multi_agent_batch_eval.py` calls `client.evals.run_inference(agent=<resource>)`;
there is no local/in-process inference path (`build_agent_info` is built but not used
for inference). To stay cheap, the gate scores the **already-deployed shared engine**
(`vars.AGENT_ENGINE_ID`), so it is a **quality-regression alarm + capability demo**,
not a strict per-diff gate. True per-diff gating would require a temp deploy per PR
(`src.pipelines.submit --agent-module`, ~15-25 min) or a new local-inference path —
deliberately out of scope. Related: [[online-eval-content-capture-blocked]] (why the
native online evaluators are platform-blocked, forcing the offline path everywhere).

## It had never run (found 2026-09-08)

Every one of this workflow's **15 invocations** between shipping (2026-08-14) and
2026-09-08 was `skipped`. Nobody ever applied the `run-eval` label, and nothing about
a run list of fifteen tidy `skipped` rows says "this check does not exist".

So everything documented above was, until that date, **unverified**: the multi-turn and
empty-at-200 smoke steps (roadmap P2.9) had never executed against a live engine, and
neither had the engine-config step — the 9-day-stale `AGENT_ENGINE_ID` it later caught
was found by a *manual* run, not by the gate.

The first real run (`workflow_dispatch`, 2026-09-08, run `34248351123`) came back
**fully green**, which is the uncomfortable part: the code was right the whole time.
Nothing was broken, so nothing would have alerted — and equally, had a flag been
renamed six weeks ago, nothing would have alerted then either.

**The fix is a cadence, not an assertion.** A weekly `schedule` (`17 9 * * 1`) now runs
it unattended. Two details are load-bearing:

* `schedule` must also appear in the **job's `if:` condition**. Adding the trigger
  alone leaves every scheduled run `skipped` — reintroducing the exact silent no-op the
  schedule exists to end, inside the fix for it. Pinned by
  `tests/test_monitoring_publish.py::TestTheEvalGateActuallyRuns`.
* The two smoke steps are `continue-on-error`, so a permanently broken one is a single
  word in a summary table nobody opens. A guard now fails the job when **both** fail —
  both, not either, because one failure is a flaky live engine and an advisory gate
  that reds on that gets ignored.

Also corrected here: `uv sync --group dev --group pipelines` asked for a group the
later bare `uv run` re-syncs away (uv's default group is `dev`, and this gate needs no
kfp). It looked like it did something and did not.

See [[checks-that-cannot-detect-their-own-failure]] — this is the purest instance in
that note: not a check returning the wrong answer, but one returning no answer,
indefinitely, while looking orderly.
