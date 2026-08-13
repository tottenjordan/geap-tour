"""Offline tests for the bake-off model-availability preflight (no network).

``preflight`` sends a 1-token completion at each candidate backbone through the
*same* serving path the deployed coordinator uses (LiteLLM → Vertex global
endpoint), so ``run_bakeoff --execute`` fails fast — before paying for two Agent
Engine deploys — if a model id isn't served. The completion call is injectable so
these tests never touch Vertex.
"""

import pytest

from src.eval import preflight


def test_check_served_ok_uses_vertex_ai_prefix_and_global():
    seen = {}

    def fake_completion(**kwargs):
        seen.update(kwargs)
        return {"ok": True}

    ok, _detail = preflight.check_model_served("gemini-3.6-flash", completion_fn=fake_completion)
    assert ok is True
    assert seen["model"] == "vertex_ai/gemini-3.6-flash"
    assert seen["vertex_location"] == "global"
    assert seen["max_tokens"] == 1


def test_check_served_keeps_existing_vertex_ai_prefix():
    seen = {}
    preflight.check_model_served(
        "vertex_ai/claude-sonnet-5",
        completion_fn=lambda **k: seen.update(k),
    )
    assert seen["model"] == "vertex_ai/claude-sonnet-5"


def test_check_served_reports_failure_without_raising():
    def boom(**kwargs):
        raise RuntimeError("404 model not found")

    ok, detail = preflight.check_model_served("nope-model", completion_fn=boom)
    assert ok is False
    assert "404 model not found" in detail


def test_preflight_models_aggregates_all():
    def fake(model, **kwargs):
        if "bad" in model:
            raise RuntimeError("not served")
        return {"ok": True}

    results = preflight.preflight_models(["good-model", "bad-model"], completion_fn=fake)
    assert results["good-model"][0] is True
    assert results["bad-model"][0] is False


def test_ensure_models_served_raises_on_any_failure():
    def fake(model, **kwargs):
        if "bad" in model:
            raise RuntimeError("not served")
        return {"ok": True}

    # All-good: returns the results dict, no raise.
    ok = preflight.ensure_models_served(["good-1", "good-2"], completion_fn=fake)
    assert all(v[0] for v in ok.values())

    # Any failure: raises with the offending model in the message.
    with pytest.raises(preflight.ModelNotServedError) as exc:
        preflight.ensure_models_served(["good-1", "bad-1"], completion_fn=fake)
    assert "bad-1" in str(exc.value)
