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


SCRIPTS = _REPO_ROOT / "scripts"


def _py_files():
    """Both src/ AND scripts/.

    The first version of this guard scanned only src/, and `scripts/generate_pptx.py`
    consequently kept a bare `GCP_PROJECT = "hybrid-vertex"` with no env path at all
    — every console deep-link in the generated deck pointed at one project
    regardless of configuration. A guard that covers half the repo reports clean on
    the other half.
    """
    return sorted(SRC.rglob("*.py")) + sorted(SCRIPTS.rglob("*.py"))


def _rel(path: pathlib.Path) -> str:
    return str(path.relative_to(_REPO_ROOT))


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
            f"{_rel(p)}:{line}"
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
            f"{_rel(p)}:{line}"
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
    ALLOWED: ClassVar[set[str]] = {"src/deploy/find_orphan_engines.py"}

    def test_no_module_embeds_a_19_digit_engine_id(self):
        offenders = []
        for p in _py_files():
            if str(_rel(p)) in self.ALLOWED:
                continue
            for line, value in _string_constants(p):
                if re.fullmatch(r"\d{19}", value.strip()):
                    offenders.append(f"{_rel(p)}:{line} = {value}")
        assert not offenders, (
            "engine ids hardcoded — read them from .env via config, and use "
            f"<AGENT_ENGINE_ID>-style placeholders in docstrings: {offenders}"
        )

    def test_the_allowlisted_module_really_does_need_it(self):
        """Guard the exemption: it holds only while that id is an allowlist KEY."""
        from src.deploy.find_orphan_engines import KNOWN_UNREFERENCED

        assert KNOWN_UNREFERENCED, "the exemption exists for this mapping's keys"
        assert all(re.fullmatch(r"\d{19}", k) for k in KNOWN_UNREFERENCED)


class TestShellScriptsAreEnvDriven:
    """Only 3 of 14 scripts ever sourced .env; the rest re-derived their own
    defaults, so a project configured in .env was ignored by most of the tooling
    that acts on it. And ten copies of a default are ten chances for one to rot —
    `setup_apphub.sh` carried a hardcoded project number next to two engine ids
    that had both been deleted."""

    @staticmethod
    def _shell_scripts():
        return sorted(SCRIPTS.glob("*.sh"))

    @staticmethod
    def _needs_gcp(text: str) -> bool:
        return "gcloud" in text or "PROJECT_ID" in text

    def test_only_the_shared_loader_names_the_project_default(self):
        offenders = []
        for p in self._shell_scripts():
            for i, line in enumerate(p.read_text().splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue  # prose explaining an example path is not a default
                if PROJECT_ID in line or PROJECT_NUMBER in line:
                    offenders.append(f"{_rel(p)}:{i}")
        assert not offenders, (
            f"hardcoded project id/number in shell scripts — source lib/config.sh: {offenders}"
        )

    def test_every_gcp_script_loads_dotenv_through_the_loader(self):
        offenders = [
            _rel(p)
            for p in self._shell_scripts()
            if self._needs_gcp(p.read_text()) and "lib/config.sh" not in p.read_text()
        ]
        assert not offenders, f"scripts using GCP but never loading .env: {offenders}"

    def test_the_loader_derives_the_project_number(self):
        """A literal project number only works in one project and fails deep inside
        an App Hub call rather than at the point of error."""
        text = (SCRIPTS / "lib" / "config.sh").read_text()
        assert "gcloud projects describe" in text
        assert "project_number()" in text

    def test_no_shell_script_embeds_an_engine_id(self):
        """The hole this class had: engine ids were guarded in `.py` only.

        `setup_governance_policies.sh` consequently kept two `:-<19 digits>` fallbacks
        pointing at DELETED engines straight through the sweep that was supposed to
        remove exactly that. Comment lines are exempt because two scripts legitimately
        *name* removed engines in prose explaining the removal — the same reasoning
        `_string_constants` applies to docstrings.
        """
        offenders = []
        for p in self._shell_scripts():
            for i, line in enumerate(p.read_text().splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if re.search(r"\d{19}", line):
                    offenders.append(f"{_rel(p)}:{i}")
        assert not offenders, (
            f"engine ids hardcoded in shell — require them via require_var: {offenders}"
        )


class TestTheGovernanceScriptCannotSilentlyPickTheWrongEngine:
    """`setup_governance_policies.sh` used to resolve the two ids like this:

        AGENT_ENGINE_ID="${COORDINATOR_AGENT_ID:-${AGENT_ENGINE_ID:-<literal>}}"
        ROUTER_ENGINE_ID="${ROUTER_ENGINE_ID:-${AGENT_ENGINE_ID:-<literal>}}"

    The second line read `AGENT_ENGINE_ID` *after* the first overwrote it with the
    coordinator's id, so an unset `ROUTER_ENGINE_ID` resolved the router TO THE
    COORDINATOR — and the script then granted `roles/agentregistry.viewer` to the
    coordinator twice, never to the router, while printing `Router ... ok`. That
    grant is the documented remediation for the router's 403 MCP-resolution
    fallback, so the failure mode was a silently un-remediated router.

    These drive the real script. They only stay offline because the id resolution
    happens BEFORE the `gcloud` calls — do not move it back below them.
    """

    SCRIPT: ClassVar[pathlib.Path] = SCRIPTS / "setup_governance_policies.sh"

    def _run(self, **env_overrides):
        import os
        import subprocess

        env = {**os.environ, **env_overrides}
        return subprocess.run(
            ["bash", str(self.SCRIPT), "--dry-run"],
            capture_output=True,
            text=True,
            env=env,
            cwd=_REPO_ROOT,
            timeout=60,
        )

    def test_a_missing_router_id_stops_the_script(self):
        """Was: silently became the coordinator's id. COORDINATOR_AGENT_ID is pinned
        so this reaches the router check whether or not a .env exists (CI has none)."""
        res = self._run(COORDINATOR_AGENT_ID="1111111111111111111", ROUTER_ENGINE_ID="")
        assert res.returncode != 0
        assert "ROUTER_ENGINE_ID" in res.stderr

    def test_a_missing_coordinator_id_stops_the_script(self):
        """The distinct message also proves the test above reached the router check
        rather than tripping over this one."""
        res = self._run(COORDINATOR_AGENT_ID="", AGENT_ENGINE_ID="")
        assert res.returncode != 0
        assert "COORDINATOR_AGENT_ID" in res.stderr
        assert "ROUTER_ENGINE_ID" not in res.stderr

    def test_it_fails_before_spending_a_network_call(self):
        """Resolution sits above `project_number()` / `gcloud auth`, so a
        misconfigured run costs nothing and works without credentials. The banner
        prints the project number, so its absence is the evidence."""
        res = self._run(COORDINATOR_AGENT_ID="1111111111111111111", ROUTER_ENGINE_ID="")
        assert "GEAP Governance Policies Setup" not in res.stdout

    def test_the_two_variables_cannot_alias(self):
        """The structural fix: the coordinator's id is never written into a name the
        router's resolution reads."""
        text = self.SCRIPT.read_text()
        body = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        assert "COORDINATOR_ENGINE_ID=" in body
        assert 'ROUTER_ENGINE_ID="$(require_var ROUTER_ENGINE_ID)"' in body
        assert "AGENT_ENGINE_ID=" not in body


class TestTheLoaderDoesNotClobberTheEnvironment:
    """`.env` fills in what the environment lacks; it must not overwrite it.

    `set -a; source .env` runs every assignment unconditionally, so
    `GCP_PROJECT_ID=my-sandbox bash scripts/setup_governance_policies.sh` silently
    granted IAM in `hybrid-vertex` instead. The loader's comment claimed the correct
    behaviour while the code did the opposite, and nothing tested it. It also has to
    agree with `src/config.py`, whose `load_dotenv()` defaults to `override=False`.

    These run against a THROWAWAY repo root with a `.env` they control, never the
    developer's. CI has no `.env`, and without one every assertion here passes for
    the wrong reason: the loader's job is to read a file, and with no file to read a
    broken loader and a correct one agree. The first version of this class was green
    in CI for exactly that reason.
    """

    @pytest.fixture
    def sandbox(self, tmp_path):
        import shutil

        (tmp_path / "scripts" / "lib").mkdir(parents=True)
        shutil.copy(SCRIPTS / "lib" / "config.sh", tmp_path / "scripts" / "lib" / "config.sh")
        (tmp_path / ".env").write_text(
            'GCP_PROJECT_ID=from-dotenv\nLABEL_KEY="solution"\nPLAIN=bare\n'
        )
        return tmp_path

    @staticmethod
    def _resolve(sandbox, var, *, unset=(), **env_overrides):
        """`unset` removes a name from the child's environment entirely.

        Passing `VAR=""` is NOT the same thing: the loader treats set-but-empty as
        set, on purpose, so the caller can blank a value deliberately. Using "" to
        mean "unset" is what made the first version of these tests fail.
        """
        import os
        import subprocess

        env = {k: v for k, v in os.environ.items() if k not in unset}
        env.update(env_overrides)
        return subprocess.run(
            ["bash", "-c", f'source scripts/lib/config.sh; printf "%s" "${var}"'],
            capture_output=True,
            text=True,
            env=env,
            cwd=sandbox,
            timeout=60,
        ).stdout

    def test_the_sandbox_dotenv_is_really_being_read(self, sandbox):
        """Non-vacuity: if the loader read nothing, the tests below would still pass
        by falling through to their defaults."""
        assert self._resolve(sandbox, "PROJECT_ID", unset=("GCP_PROJECT_ID",)) == "from-dotenv"

    def test_an_explicit_variable_wins_over_the_file(self, sandbox):
        """THE regression. Was: the file clobbered it and IAM landed elsewhere."""
        assert self._resolve(sandbox, "PROJECT_ID", GCP_PROJECT_ID="some-other-project") == (
            "some-other-project"
        )

    def test_the_file_still_fills_in_what_is_unset(self, sandbox):
        assert self._resolve(sandbox, "PROJECT_ID", unset=("GCP_PROJECT_ID",)) == "from-dotenv"

    def test_a_deliberately_blanked_variable_is_respected(self, sandbox):
        """Set-but-empty counts as set, so `.env` does not quietly refill it. This is
        also what lets a caller force a script's own error path."""
        assert self._resolve(sandbox, "LABEL_KEY", LABEL_KEY="") == ""

    def test_quoted_values_are_unquoted_as_sourcing_did(self, sandbox):
        """`.env` holds `LABEL_KEY="solution"`; a naive line-splitting reader would
        export the quotes along with the value."""
        assert self._resolve(sandbox, "LABEL_KEY", unset=("LABEL_KEY",)) == "solution"
        assert self._resolve(sandbox, "PLAIN", unset=("PLAIN",)) == "bare"

    def test_python_and_bash_agree(self, sandbox):
        """The two halves of the repo resolved the same variable differently: Python
        honoured the environment, bash let the file win.

        Bash reads the sandbox `.env` (which sets a *different* project, so a
        regressed loader would disagree); Python reads the repo as it normally does.
        Both must return what the environment asked for."""
        import os
        import subprocess
        import sys

        env = {**os.environ, "GCP_PROJECT_ID": "some-other-project"}
        # sys.executable, not `uv run` — this must not depend on uv resolving a venv
        # inside a test, and a failure here would otherwise compare "" to a real value.
        proc = subprocess.run(
            [sys.executable, "-c", "import src.config as c; print(c.GCP_PROJECT_ID)"],
            capture_output=True,
            text=True,
            env=env,
            cwd=_REPO_ROOT,
            timeout=180,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == self._resolve(
            sandbox, "PROJECT_ID", GCP_PROJECT_ID="some-other-project"
        )


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

    def test_the_shell_scanner_finds_a_planted_engine_id(self, tmp_path, monkeypatch):
        """The shell engine-id check is new, and a scanner that matches nothing
        passes for free — which is exactly how the `.py`-only version reported clean
        on `setup_governance_policies.sh` for months."""
        script = tmp_path / "planted.sh"
        script.write_text('ID="${ROUTER_ENGINE_ID:-6023683798619652096}"\n')
        monkeypatch.setattr(
            TestShellScriptsAreEnvDriven, "_shell_scripts", staticmethod(lambda: [script])
        )
        monkeypatch.setattr("tests.test_no_hardcoded_values._rel", lambda p: p.name)
        with pytest.raises(AssertionError, match="engine ids hardcoded in shell"):
            TestShellScriptsAreEnvDriven().test_no_shell_script_embeds_an_engine_id()

    def test_the_shell_scanner_ignores_a_commented_id(self, tmp_path, monkeypatch):
        """Two scripts explain a removal by naming the removed engine."""
        script = tmp_path / "commented.sh"
        script.write_text("# 8296365537139621888 was deleted; do not reintroduce it\n")
        monkeypatch.setattr(
            TestShellScriptsAreEnvDriven, "_shell_scripts", staticmethod(lambda: [script])
        )
        TestShellScriptsAreEnvDriven().test_no_shell_script_embeds_an_engine_id()
