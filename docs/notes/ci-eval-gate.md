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
