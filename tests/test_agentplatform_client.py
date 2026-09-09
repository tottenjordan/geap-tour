"""No `vertexai.Client` (or `vertexai` eval types) anywhere in src/.

`vertexai.Client` emits a FutureWarning pointing at `agentplatform.Client` and will
eventually be removed. `agentplatform` ships inside the same
`google-cloud-aiplatform>=1.163.0` distribution already pinned by pyproject.toml and
deploy_agents.REQUIREMENTS, so this costs no dependency change.

`types` is covered alongside `Client` because the two packages are **separate
copies**, not aliases (`agentplatform.types is vertexai.types` -> False). Handing a
`vertexai.types.evals.*` object to an `agentplatform` client mixes pydantic class
hierarchies, and `_sdk_patches._flip_extra_to_ignore` would have flipped the wrong
package's models.

Deliberately NOT covered: `vertexai.init`. It is the same function object as
`agentplatform.init`, and migrating it was tried on 2026-09-08 and reverted — 12 of
17 callers also need `vertexai.agent_engines`, and `agentplatform.init` types as
`Callable | None` (it degrades to None on ImportError) so `ty` reddens every call
site. See `TestInitDeliberatelyStaysOnVertexai`, which pins those reasons.

Still NOT covered: `vertexai.agent_engines` / `AdkApp`. `agentplatform` has no
`agent_engines` attribute at all (checked against google-cloud-aiplatform 2.1.0 on
2026-09-08, and re-checked by a test so a future release makes the exception
removable), and the AdkApp instance is cloudpickled into the served engine.

See docs/notes/agentplatform-client-migration.md.
"""

import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"
BANNED = re.compile(r"vertexai\.Client\(|from vertexai import (Client|types)\b")


def test_no_vertexai_client_or_types_in_src():
    hits = [
        f"{p.relative_to(SRC.parent)}:{i}: {line.strip()}"
        for p in sorted(SRC.rglob("*.py"))
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if BANNED.search(line)
    ]
    assert not hits, "use agentplatform.Client / agentplatform types:\n" + "\n".join(hits)


def test_patches_target_the_same_package_as_the_client():
    """A client that moves without its patches silently loses them.

    `agentplatform._genai._evals_common` is a separate module object, so patching
    `vertexai._genai` while constructing an `agentplatform.Client` reinstates the
    "Failed to parse agent run response" bug that collapses every metric to ~0.
    """
    from src.eval import multi_agent_batch_eval as mabe

    assert mabe.Client.__module__.startswith("agentplatform.")
    assert mabe.types.__name__.startswith("agentplatform.")


def test_agentplatform_client_does_not_warn():
    """The whole point: constructing the client is FutureWarning-free."""
    import warnings

    import agentplatform

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        agentplatform.Client(project="test-project", location="us-central1")

    assert [str(w.message) for w in caught if issubclass(w.category, FutureWarning)] == []


class TestInitDeliberatelyStaysOnVertexai:
    """`vertexai.init` is NOT migrated to `agentplatform.init`, and that is measured.

    Both names are the same function object (`vertexai.init is agentplatform.init`
    -> True; both re-export `google.cloud.aiplatform.init`), so the move is
    behaviourally free. It was tried on 2026-09-08 and reverted, for two reasons
    found by doing it:

      1. Of the 17 files calling `vertexai.init`, **12 also use
         `vertexai.agent_engines`**, which has no `agentplatform` equivalent.
         Switching those would import BOTH packages to call one function.
      2. For the other 5, `ty` went red. `vertexai/__init__.py` imports `init`
         unconditionally, while `agentplatform/__init__.py` wraps it in
         `try: ... except ImportError: init = None` — so `agentplatform.init` types
         as `Callable | None` and every call site becomes `call-non-callable`.

    So `agentplatform.init` is strictly worse for zero gain. These tests pin the
    *reasons*, so the next person does not repeat the experiment.
    """

    def test_the_two_names_are_the_same_function(self):
        """If this ever stops being true, the choice is no longer free and the
        whole rationale below needs revisiting."""
        import agentplatform
        import vertexai

        assert vertexai.init is agentplatform.init

    def test_agentplatform_init_is_the_optional_one(self):
        """Reason 2, as a fact rather than a claim: agentplatform degrades `init`
        to None when aiplatform is missing, and vertexai does not."""
        import inspect

        import agentplatform

        src = inspect.getsource(agentplatform)
        assert "init = None" in src, (
            "agentplatform no longer degrades init to None — the ty objection is "
            "gone and migrating init is worth re-testing"
        )

    def test_most_init_callers_genuinely_need_vertexai_anyway(self):
        """Reason 1. Not a hard threshold — just enough to keep the claim honest if
        the ratio ever inverts."""
        callers = [p for p in SRC.rglob("*.py") if "vertexai.init(" in p.read_text()]
        also_engines = [p for p in callers if "agent_engines" in p.read_text()]
        assert len(callers) >= 10, "expected init to be widely used; recheck the note"
        assert len(also_engines) > len(callers) / 2, (
            f"only {len(also_engines)}/{len(callers)} init callers still need "
            "vertexai.agent_engines — migrating init may now be worthwhile"
        )

    def test_agentplatform_still_has_no_agent_engines(self):
        """The fact the exception rests on. Checked against
        google-cloud-aiplatform 2.1.0 on 2026-09-08, and re-checked on every SDK
        bump — if a release adds it, the remaining vertexai imports can migrate and
        this test says so by failing."""
        import agentplatform

        assert not hasattr(agentplatform, "agent_engines"), (
            "agentplatform grew agent_engines — the remaining vertexai imports can "
            "now migrate, and this test should be deleted"
        )


def test_the_preview_evaluation_namespace_still_exists():
    """`src/eval/trajectory_eval.py` imports `vertexai.preview.evaluation.EvalTask`.

    A *preview* namespace with no `agentplatform` equivalent — the likeliest
    casualty of an SDK major bump, and it survived aiplatform 2.x by luck rather
    than by contract. Without this test its removal would surface as trajectory
    metrics quietly missing from a report, which is the failure mode this repo keeps
    finding, rather than as one obvious red test.
    """
    from vertexai.preview.evaluation import EvalTask

    assert EvalTask is not None


class TestAgentEnginesGetIsCalledPositionally:
    """`vertexai.agent_engines.get` takes a POSITIONAL resource name.

    `client.agent_engines.get(name=...)` (aiplatform 1.x, now removed) and
    `vertexai.agent_engines.get(resource_name)` are different functions with
    different signatures. Migrating the former to the latter by swapping the module
    prefix — and keeping `name=` — produces:

        TypeError: get() got an unexpected keyword argument 'name'

    Six call sites were migrated that way. FIVE of them sit inside best-effort
    `try/except Exception` warm-up blocks, so the failure surfaced only as
    "Warmup skipped: ..." — a silent loss of the cold-start protection that exists
    to stop empty-at-200, with no test failing and no error reaching a human.
    """

    def test_no_call_site_passes_name_as_a_keyword(self):
        offenders = [
            f"{p.relative_to(SRC.parent)}:{i}"
            for p in sorted(SRC.rglob("*.py"))
            for i, line in enumerate(p.read_text().splitlines(), 1)
            if "agent_engines.get(name=" in line
        ]
        assert not offenders, (
            "vertexai.agent_engines.get takes a positional resource_name; `name=` "
            f"raises TypeError and warm-up blocks swallow it: {offenders}"
        )

    def test_the_real_signature_is_what_we_assume(self):
        """Pin the assumption itself, so a future SDK change is a red test rather
        than another silently-skipped warm-up."""
        import inspect

        from vertexai import agent_engines

        params = inspect.signature(agent_engines.get).parameters
        assert "resource_name" in params
        assert "name" not in params
