# Code Standards

Standards that must be adhered to in this project when writing code and making
environment changes. Refer to this document (and keep it current) whenever you
touch code or configuration.

## Git & commits

- **Never** add `Co-Authored-By` trailers to commits or PRs.
- Branch before committing when on `main`. Once a PR is open and you are
  actively working on it, commit and push to that PR as needed so the user can
  review commits as they land — you do not need to ask before each commit/push.
  **Never** merge or mark a PR approved: PR approval/merge always requires the
  user's review.

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
