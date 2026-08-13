"""Model-availability preflight for the coordinator bake-off.

``resolve_model()`` only branches on a string *prefix* — it never checks that
``gemini-3.6-flash`` / ``claude-sonnet-5`` are actually served in the project
(Claude on Vertex additionally needs Model Garden enablement). So a bake-off can
happily deploy two engines, spend real money, and only then discover a backbone
404s. This module sends a **1-token completion** at each model through the *same*
serving path the deployed coordinator uses — LiteLLM against the Vertex
``global`` endpoint (both bake-off backbones go through
``LiteLlm(vertex_location="global")``) — so ``run_bakeoff --execute`` can fail
fast, before the deploys.

The completion call is injectable (``completion_fn``) so it is fully unit-testable
without touching Vertex.
"""

from __future__ import annotations

from src.config import GCP_PROJECT_ID

# Both bake-off backbones require the global endpoint (see config.resolve_model).
_PREFLIGHT_LOCATION = "global"


class ModelNotServedError(RuntimeError):
    """Raised when one or more required models fail the availability check."""


def _default_completion_fn():
    import litellm

    return litellm.completion


def check_model_served(
    model_id: str,
    *,
    project: str | None = None,
    location: str = _PREFLIGHT_LOCATION,
    completion_fn=None,
) -> tuple[bool, str]:
    """Return ``(served, detail)`` for one model via a 1-token completion.

    ``served`` is False (never raises) when the call errors, with ``detail``
    carrying the exception text so the caller can report which backbone is
    missing. Mirrors ``resolve_model``'s ``vertex_ai/`` prefixing.
    """
    completion_fn = completion_fn or _default_completion_fn()
    model_str = model_id if model_id.startswith("vertex_ai/") else f"vertex_ai/{model_id}"
    try:
        completion_fn(
            model=model_str,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            vertex_project=project or GCP_PROJECT_ID,
            vertex_location=location,
        )
    except Exception as e:
        # Any failure (404, auth, quota) means "treat as not served".
        return False, f"{type(e).__name__}: {e}"
    return True, "ok"


def preflight_models(
    model_ids,
    *,
    project: str | None = None,
    completion_fn=None,
) -> dict[str, tuple[bool, str]]:
    """Check each model id; return ``{model_id: (served, detail)}``."""
    return {
        model_id: check_model_served(model_id, project=project, completion_fn=completion_fn)
        for model_id in model_ids
    }


def ensure_models_served(
    model_ids,
    *,
    project: str | None = None,
    completion_fn=None,
) -> dict[str, tuple[bool, str]]:
    """Preflight all models; raise :class:`ModelNotServedError` if any fail.

    Returns the results dict on success so callers can log the ``ok`` details.
    """
    results = preflight_models(model_ids, project=project, completion_fn=completion_fn)
    failed = {m: detail for m, (served, detail) in results.items() if not served}
    if failed:
        lines = "; ".join(f"{m} → {detail}" for m, detail in failed.items())
        raise ModelNotServedError(
            f"preflight failed for {len(failed)} model(s): {lines}. "
            "Confirm the served id (Claude needs Model Garden enablement) or "
            "re-run with --skip-preflight to bypass."
        )
    return results


def main(argv: list[str] | None = None) -> int:
    """CLI: check that the given (or the bake-off) model ids are served."""
    import argparse

    from src.doe.run_bakeoff import bakeoff_model_ids

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "models",
        nargs="*",
        help="Model ids to check (default: the bake-off baseline + candidate)",
    )
    args = parser.parse_args(argv)
    models = args.models or list(bakeoff_model_ids())

    print(f"Preflight: checking {models} are served on the Vertex global endpoint…")
    results = preflight_models(models)
    ok = True
    for model_id, (served, detail) in results.items():
        mark = "✓" if served else "✗"
        print(f"  {mark} {model_id}: {detail}")
        ok = ok and served
    if not ok:
        print("Preflight FAILED — see the ✗ rows above.")
        return 1
    print("Preflight OK — both backbones served.")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
