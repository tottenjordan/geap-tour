"""DEPRECATED — use ``src.eval.setup_online_evaluators`` instead.

This module was a misnamed one-shot that ran a single ad-hoc evaluation pass and
called it "online monitoring". Continuous online evaluation is now a single
canonical flow built on native Online Evaluators:

    uv run python -m src.eval.setup_online_evaluators create   # set up monitor
    uv run python -m src.eval.verify_monitors                  # read results

This file is kept as a thin shim so existing invocations and imports keep
working: it delegates to the canonical setup and still exports
``QUICK_EVAL_CASES`` (consumed by ``src.eval.failure_clusters``).
"""

import sys

import src.eval.setup_online_evaluators as canonical

# Retained for src.eval.failure_clusters, which reuses these demo prompts.
QUICK_EVAL_CASES = [
    "Find me a hotel in Miami",
    "Search for hotels in New York under $350",
    "Check if a $50 meal expense is within policy",
    "Check policy for a $500 entertainment expense",
    "Submit a $45 meals expense for lunch meeting, user ID EMP001",
]

_DEPRECATION_NOTICE = (
    "[DEPRECATED] src.eval.setup_online_monitors has been retired. "
    "Delegating to the canonical native online evaluator setup "
    "(src.eval.setup_online_evaluators create). "
    "Use `uv run python -m src.eval.setup_online_evaluators create` directly."
)


def main(argv: list[str] | None = None) -> None:
    """Print a deprecation notice and delegate to the canonical setup."""
    argv = list(sys.argv[1:] if argv is None else argv)
    print(_DEPRECATION_NOTICE)
    sample_rate = int(argv[0]) if argv and argv[0].isdigit() else 100
    canonical.create_evaluators(sample_rate=sample_rate)


if __name__ == "__main__":
    main()
