"""Engine warmup has to actually run — and ``import vertexai`` is not enough for it.

``import vertexai`` does NOT load the ``vertexai.agent_engines`` submodule, so
``vertexai.agent_engines.get(...)`` raises ``AttributeError`` unless something else
in the process happened to import it first. Six call sites were written that way
(#106). Five were warmups inside a best-effort ``try``: the batch eval printed
``Warmup skipped: module 'vertexai' has no attribute 'agent_engines'`` on every CI
run for three weeks, and the judges swallowed it with ``except: pass``. The sixth,
``run_bakeoff._measure_usage``, was uncaught — an ``--execute`` bake-off would have
died at the cost step after deploying and scoring both engines.

The test suite could not see any of it: ``deploy_agents`` does
``from vertexai import agent_engines`` and pytest imports it early, so the
attribute is bound in this process. That is why the behavioural checks here run in
a FRESH interpreter, and why the guard is structural rather than behavioural.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]


def _fresh(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=_REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestThePremise:
    def test_import_vertexai_does_not_bind_agent_engines(self):
        """If this ever starts failing, the SDK now binds the submodule on package
        import and the guard below is merely belt-and-braces."""
        proc = _fresh("import vertexai; print(hasattr(vertexai, 'agent_engines'))")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "False"

    def test_the_eval_entrypoints_do_not_bind_it_either(self):
        """The bug survived because the CLIs never import it as a side effect."""
        proc = _fresh(
            """
            import src.eval.multi_agent_batch_eval, src.doe.run_bakeoff, vertexai
            print(hasattr(vertexai, 'agent_engines'))
            """
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip().splitlines()[-1] == "False"


class TestWarmEngineReachesTheEngine:
    def test_in_a_fresh_process(self):
        """The real regression test. A fake submodule sits in ``sys.modules`` but is
        NOT bound on the package — exactly the state of a fresh CLI process. The old
        ``vertexai.agent_engines.get`` spelling raises AttributeError here; ``from
        vertexai import agent_engines`` resolves it."""
        proc = _fresh(
            """
            import sys, types
            import vertexai
            from src.eval._sdk_patches import warm_engine

            class Engine:
                def stream_query(self, **_k):
                    yield {"content": {"parts": [{"text": "pong"}]}}

            seen = []
            fake = types.ModuleType("vertexai.agent_engines")
            fake.get = lambda name: seen.append(name) or Engine()
            sys.modules["vertexai.agent_engines"] = fake
            assert not hasattr(vertexai, "agent_engines")

            print(warm_engine("projects/p/locations/l/reasoningEngines/1", n=2), seen)
            """
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "2 ['projects/p/locations/l/reasoningEngines/1']"

    def test_a_lookup_failure_propagates(self, monkeypatch):
        """Callers decide how to report a skip; the helper must not hide one."""
        from vertexai import agent_engines

        from src.eval._sdk_patches import warm_engine

        def _boom(_name):
            raise RuntimeError("engine not found")

        monkeypatch.setattr(agent_engines, "get", _boom)
        with pytest.raises(RuntimeError, match="engine not found"):
            warm_engine("projects/p/locations/l/reasoningEngines/1")


class TestTheBakeoffCostStep:
    def test_measure_usage_with_an_injected_client(self, monkeypatch):
        """Was a NameError: ``vertexai`` was only bound inside ``if client is None``."""
        from vertexai import agent_engines

        from src.doe import run_bakeoff

        monkeypatch.setattr(agent_engines, "get", lambda name: f"engine:{name}")
        monkeypatch.setattr(
            run_bakeoff, "collect_token_usage", lambda engine, prompts: [{engine: len(prompts)}]
        )
        got = run_bakeoff._measure_usage(
            "E1", "gemini", client=object(), cases=[{"prompt": "a"}, {"prompt": "b"}]
        )
        assert got == [{"engine:E1": 2}]


def _attribute_access_sites() -> list[str]:
    hits = []
    for py in sorted((_REPO / "src").rglob("*.py")):
        for node in ast.walk(ast.parse(py.read_text(), filename=str(py))):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "agent_engines"
                and isinstance(node.value, ast.Name)
                and node.value.id == "vertexai"
            ):
                hits.append(f"{py.relative_to(_REPO)}:{node.lineno}")
    return hits


class TestNoCallSiteReliesOnAnImportSideEffect:
    def test_no_vertexai_dot_agent_engines_in_src(self):
        """Structural, because behaviour depends on what ELSE the process imported —
        which is how six sites passed every test while broken."""
        hits = _attribute_access_sites()
        assert not hits, (
            f"`vertexai.agent_engines` used as an attribute at {hits}. `import vertexai` "
            "does not load that submodule; write `from vertexai import agent_engines` "
            "(or call `_sdk_patches.warm_engine`)."
        )
