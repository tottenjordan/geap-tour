"""Deployment-specific values must come from config, not from literals in code.

Every hardcoded identifier found in this repo has eventually gone stale, and none
of them failed loudly when they did:

* `setup_apphub.sh` defaulted to two engine ids that had both been **deleted** — an
  unset env var did not error, it registered non-existent engines in App Hub.
* `src/config.py`'s own `AGENT_ENGINE_ID` / `ROUTER_ENGINE_ID` defaults were dead
  engines, so a checkout without `.env` pointed every consumer at ghosts.
* `pipelines/components.py` hardcoded project, region *and* image tag, so the
  pipeline could only run in one project and `build_eval_image.sh v4` silently kept
  executing v3.
* `submit.py` hardcoded a project-number service account.

The pattern is always the same: a literal that is correct on the day it is written
and unfalsifiable afterwards. These are cheap AST/text checks over committed source
— they cannot tell a *live* id from a dead one, only that the code is asking config
instead of asserting an answer.
"""

from __future__ import annotations

import ast
import pathlib
import re
from typing import ClassVar

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = _REPO_ROOT / "src"

# The project this repo is developed against. A literal here is not automatically
# wrong — it is the documented default — but it must never be the ONLY source.
PROJECT_ID = "hybrid-vertex"
PROJECT_NUMBER = "934903580331"


def _py_files():
    return sorted(SRC.rglob("*.py"))


def _string_constants(path: pathlib.Path):
    """(lineno, value) for every string literal, so comments/docstrings are skipped.

    Module/class/function docstrings ARE ast.Constant, so they are filtered
    explicitly — several of these values are legitimately *discussed* in prose that
    explains why they were removed, and a grep-based check fails on its own
    documentation (learned the hard way in tests/test_auth.py).
    """
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in docstrings:
                continue
            yield node.lineno, node.value


class TestNoHardcodedProjectIdentifiers:
    def test_the_project_id_appears_only_as_a_config_default(self):
        """`GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "hybrid-vertex")` is
        the one legitimate home. Anywhere else and a fork silently talks to our
        project — or, more likely, fails with a permission error nobody expects."""
        offenders = [
            f"{p.relative_to(SRC)}:{line}"
            for p in _py_files()
            if p.name != "config.py"
            for line, value in _string_constants(p)
            if PROJECT_ID in value
        ]
        assert not offenders, (
            f"project id hardcoded outside config.py — read GCP_PROJECT_ID: {offenders}"
        )

    def test_the_project_number_is_never_embedded_in_a_literal(self):
        """A project-number service account (`934903580331-compute@...`) only works
        in one project, and a wrong SA fails deep inside a PipelineJob rather than
        at submit time. Derive it from PROJECT_NUMBER."""
        offenders = [
            f"{p.relative_to(SRC)}:{line}"
            for p in _py_files()
            for line, value in _string_constants(p)
            if PROJECT_NUMBER in value
        ]
        assert not offenders, (
            f"project number hardcoded — derive from config.PROJECT_NUMBER: {offenders}"
        )


class TestNoHardcodedEngineIds:
    """An engine id in code cannot be told from a deleted one by reading it."""

    # An id IS the key of the allowlist, and the module docstrings deliberately name
    # the engines they were written about. Both are recorded here so the exemption
    # is visible rather than implicit in a loose pattern.
    ALLOWED: ClassVar[set[str]] = {"deploy/find_orphan_engines.py"}

    def test_no_module_embeds_a_19_digit_engine_id(self):
        offenders = []
        for p in _py_files():
            if str(p.relative_to(SRC)) in self.ALLOWED:
                continue
            for line, value in _string_constants(p):
                if re.fullmatch(r"\d{19}", value.strip()):
                    offenders.append(f"{p.relative_to(SRC)}:{line} = {value}")
        assert not offenders, (
            "engine ids hardcoded — read them from .env via config, and use "
            f"<AGENT_ENGINE_ID>-style placeholders in docstrings: {offenders}"
        )

    def test_the_allowlisted_module_really_does_need_it(self):
        """Guard the exemption: it holds only while that id is an allowlist KEY."""
        from src.deploy.find_orphan_engines import KNOWN_UNREFERENCED

        assert KNOWN_UNREFERENCED, "the exemption exists for this mapping's keys"
        assert all(re.fullmatch(r"\d{19}", k) for k in KNOWN_UNREFERENCED)


class TestDeploymentOutputsAreWrittenBack:
    """A value a deploy produces but does not record has to be copy-pasted, and a
    value nobody copies goes stale. `.env` is the source of truth, so deploys write
    to it."""

    def test_agent_deploys_write_their_engine_id(self):
        import inspect

        from src.deploy import deploy_agents

        assert "_update_env_file" in inspect.getsource(deploy_agents.run_deploy)

    def test_mcp_deploys_write_their_urls(self):
        import inspect

        from src.deploy import deploy_mcp_servers

        assert "set_env_var" in inspect.getsource(deploy_mcp_servers.deploy_all_servers)

    def test_both_use_the_one_shared_writer(self):
        """Two copies of "edit a line in .env" drift. The first one applied
        split("/")[-1] to everything, which silently strips the scheme off a URL."""
        for module in ("src/deploy/deploy_agents.py", "src/deploy/deploy_mcp_servers.py"):
            assert "env_file" in (_REPO_ROOT / module).read_text(), module


class TestGuardsAreNotVacuous:
    """A scanner that matches nothing passes every check above for free. This repo
    has already shipped one guard that did exactly that."""

    @pytest.mark.parametrize(
        ("source", "needle"),
        [
            ('X = "hybrid-vertex"', PROJECT_ID),
            (f'SA = "{PROJECT_NUMBER}-compute@developer.gserviceaccount.com"', PROJECT_NUMBER),
            ('E = "4709107696450666496"', "4709107696450666496"),
        ],
    )
    def test_the_scanner_finds_a_literal_it_should(self, tmp_path, source, needle):
        p = tmp_path / "probe.py"
        p.write_text(source + "\n")
        assert any(needle in v for _, v in _string_constants(p))

    def test_the_scanner_ignores_the_same_value_in_a_docstring(self, tmp_path):
        """Otherwise this very file, and every note explaining the removals, fails."""
        p = tmp_path / "probe.py"
        p.write_text('"""We used to hardcode hybrid-vertex and 934903580331 here."""\n')
        assert not [v for _, v in _string_constants(p) if PROJECT_ID in v]

    def test_it_actually_reads_files(self):
        """If _py_files() returned nothing the whole module would pass silently."""
        assert len(_py_files()) > 50
