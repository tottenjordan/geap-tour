"""Tests for the shared deterministic + retrying judge client."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.eval.judge_client import (
    build_judge_generate_fn,
    generate_with_retry,
    resolve_judge_location,
)


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text


def test_generate_with_retry_returns_first_nonempty() -> None:
    calls = {"n": 0}

    def call():
        calls["n"] += 1
        return _Resp("Score: 4")

    slept: list[float] = []
    out = generate_with_retry(call, sleep=slept.append)

    assert out == "Score: 4"
    assert calls["n"] == 1  # succeeded first try
    assert slept == []  # no backoff on success


def test_generate_with_retry_retries_on_empty_then_succeeds() -> None:
    responses = [_Resp(""), _Resp("   "), _Resp("Score: 5")]
    calls = {"n": 0}

    def call():
        r = responses[calls["n"]]
        calls["n"] += 1
        return r

    slept: list[float] = []
    out = generate_with_retry(call, max_attempts=3, backoff_s=1.0, sleep=slept.append)

    assert out == "Score: 5"
    assert calls["n"] == 3
    assert slept == [1.0, 2.0]  # linear backoff between the 3 attempts


def test_generate_with_retry_retries_on_exception_then_succeeds() -> None:
    calls = {"n": 0}

    def call():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient 503")
        return _Resp("Score: 3")

    out = generate_with_retry(call, max_attempts=3, sleep=lambda _s: None)

    assert out == "Score: 3"
    assert calls["n"] == 2


def test_generate_with_retry_exhausts_to_empty_string() -> None:
    def call():
        raise RuntimeError("always down")

    out = generate_with_retry(call, max_attempts=3, sleep=lambda _s: None)

    assert out == ""  # caller drops empty verdicts from the mean


def test_generate_with_retry_empty_forever_returns_empty() -> None:
    def call():
        return _Resp("")

    out = generate_with_retry(call, max_attempts=2, sleep=lambda _s: None)

    assert out == ""


def test_build_judge_generate_fn_pins_temperature_zero_and_strips() -> None:
    seen: dict = {}

    class _Models:
        def generate_content(self, *, model, contents, config):
            seen["model"] = model
            seen["contents"] = contents
            seen["temperature"] = config.temperature
            seen["afc"] = config.automatic_function_calling
            return _Resp("  Score: 2  ")

    fake_client = SimpleNamespace(models=_Models())

    gen = build_judge_generate_fn("gemini-2.5-flash", client=fake_client)
    out = gen("rate this")

    assert out == "Score: 2"  # stripped
    assert seen["model"] == "gemini-2.5-flash"
    assert seen["contents"] == "rate this"
    assert seen["temperature"] == 0.0  # deterministic by default
    # AFC off: the judge has no tools, and genai's default-on AFC costs a deep
    # config copy per call plus log noise (docs/notes/genai-afc-warning.md).
    assert seen["afc"].disable is True


def test_build_judge_generate_fn_retries_via_client() -> None:
    attempts = {"n": 0}

    class _Models:
        def generate_content(self, *, model, contents, config):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return _Resp("")  # transient empty
            return _Resp("Score: 4")

    fake_client = SimpleNamespace(models=_Models())

    gen = build_judge_generate_fn(
        "gemini-2.5-flash", client=fake_client, max_attempts=3, sleep=lambda _s: None
    )
    assert gen("x") == "Score: 4"
    assert attempts["n"] == 2


def test_resolve_judge_location_forces_global_for_gemini_3() -> None:
    # Gemini-3.x is only served on the global endpoint (regional 404s), so the
    # requested location is overridden — mirrors src.config.resolve_model.
    assert resolve_judge_location("gemini-3.5-flash") == "global"
    assert resolve_judge_location("gemini-3.5-flash", "us-central1") == "global"


def test_resolve_judge_location_honors_region_for_gemini_2() -> None:
    from src.config import GCP_REGION

    assert resolve_judge_location("gemini-2.5-flash") == GCP_REGION
    assert resolve_judge_location("gemini-2.5-flash", "us-west1") == "us-west1"
    assert resolve_judge_location("models/gemini-2.0-flash") == GCP_REGION


def test_build_judge_generate_fn_targets_global_for_gemini_3() -> None:
    seen: dict = {}

    class _Models:
        def generate_content(self, *, model, contents, config):
            return _Resp("Score: 4")

    def fake_genai_client(*, vertexai, project, location):
        seen["location"] = location
        return SimpleNamespace(models=_Models())

    import src.eval.judge_client as jc

    with pytest.MonkeyPatch.context() as mp:
        # Patch the lazily-imported genai.Client so no real client is built.
        import google.genai as genai

        mp.setattr(genai, "Client", fake_genai_client)
        gen = build_judge_generate_fn("gemini-3.5-flash")
        assert gen("rate this") == "Score: 4"

    assert seen["location"] == "global"
    assert jc  # module imported for coverage of the real build path


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
