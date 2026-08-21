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

Deliberately NOT covered: `vertexai.init` (not deprecated; `agentplatform.init` is
literally the same function object) and `vertexai.agent_engines` / `AdkApp` (not
deprecated, and the AdkApp instance is cloudpickled into the served engine).

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
