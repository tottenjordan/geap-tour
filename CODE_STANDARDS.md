# Code Standards

Standards that must be adhered to in this project when writing code and making
environment changes. Refer to this document (and keep it current) whenever you
touch code or configuration.

## Git & commits

- **Never** add `Co-Authored-By` trailers to commits or PRs.
- **Every code change reaches `main` through a PR. No exceptions.** Branch before
  committing when on `main`. Once a PR is open and you are actively working on
  it, commit and push to that PR as needed so the user can review commits as they
  land — you do not need to ask before each commit/push. **Never** merge or mark a
  PR approved: PR approval/merge always requires the user's review.
- **Never push directly to `main`** — not `git push origin main`, not
  `git push origin HEAD:main`, not a hotfix, not a one-line follow-up to a PR that
  just merged. This has actually happened here: two fixes (`59d3aa6`, `fec355c`)
  went straight to `main` during post-merge deployment work, skipping review
  entirely. "It's a small fix and the branch is already merged" is exactly the
  rationalisation to distrust — a follow-up fix is a *new* change and needs a
  *new* branch and PR. If a change feels too urgent to review, say so and let the
  user decide; do not decide it unilaterally.

## Python tooling

- **Package management: `uv` for everything.** Never invoke bare `pip` or
  `python`. Use `uv add` / `uv remove` to manage dependencies (don't hand-edit
  dependency lists in `pyproject.toml`), `uv sync` to install, and `uv run
  <cmd>` to run anything in the project environment. Never manually activate a
  virtualenv.
- **Lint + format: `ruff`** for both linting and formatting. Never use
  black, flake8, or isort. (`uv run ruff check` / `uv run ruff format`.)
- **Type checking: `ty`** (from Astral). Never use mypy or pyright.
- **Testing: `pytest`.** Run with `uv run pytest`.
- Target `requires-python = ">=3.11"`; keep a `src/` layout.
- For standalone scripts, prefer PEP 723 inline metadata over
  `requirements.txt`.

See the `modern-python` skill for the full rationale and command reference.

## Testing: exercise the wiring, and the default path

Two rules, both learned the expensive way in this repo. Four separate defects in one
week shared these shapes; every one of them shipped with a green suite.

- **Invoke the code the way production invokes it.** If a shell script runs a program,
  the test must run it through that same invocation — not a reconstruction of it. A
  harness that rebuilds the call cannot see a bug *in* the call.

  `setup_governance_policies.sh` ran an embedded Python block as
  `printf … | python3 - "$file" <<'PY'`. `python3 -` reads its *program* from stdin and
  the heredoc was already supplying it, so the block's `json.load(sys.stdin)` got an
  empty stream and the guard never once ran its comparison. The tests wrote the block to
  a file and ran `python3 block.py policy.json` with data on stdin — a different
  invocation, and **the difference was the defect**. Fixing that harness then missed the
  *next* bug, an inverted `if ! cmd; then rc=0; else rc=$?` that mapped "foreign binding
  found" to "clean", because the tests drove the Python but not the bash that interprets
  its result. Same gap, one layer out.

  So: extract the real invocation from the source and execute it, or execute the real
  function with its dependencies stubbed. Assert on what it *did*, not on a copy of what
  it should do.

- **Test the default path, not just the configured one.** The path with no flags set is
  the path almost everyone takes, and it is the easiest one to never run.

  `EXTRA_EGRESS_ENGINE_IDS` was exercised only with the variable *set* — a dry run and a
  live apply, both green, both merged. Unset, `${VAR//,/ }` is fatal under `set -u`, so
  every ordinary run of the script died before doing anything. Cover unset, empty, and
  each documented input form.

**Mutation-check any guard you add.** Reintroduce the defect it exists to catch and
confirm the suite goes red. A guard that passes on the broken code is worse than none —
it is a claim of coverage. This is also how to discover that a test only covers the
payload: mutate the *wiring* and watch it stay green.

## Linting posture

`ruff` is configured with a pragmatic curated ruleset (`E, F, W, I, UP, B, SIM,
C4, PIE, RUF`; `E501` ignored) rather than `select = ["ALL"]`, because this is
an existing codebase. As of 2026-08-05 the repo has a lint/type backlog (~102
ruff findings, ~47 ty diagnostics) that has **not** been auto-fixed — new/edited
code should be clean, and the backlog can be cleaned up in dedicated passes.
`select = ["ALL"]` remains the aspiration for new standalone packages.

## Known deviations (as of 2026-08-05)

Re-verify before acting — these may have changed since:

- `pyproject.toml` uses the `hatchling` build backend; `uv_build` is preferred
  for most cases. (Left as-is: current build uses a non-standard `packages =
  ["src"]` layout that would need restructuring to switch backends.)
