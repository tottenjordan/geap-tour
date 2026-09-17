"""A shell script must not claim it did something it did not do.

This file exists because one idiom produced the same defect in three scripts, and in
each of them it ran undetected for months:

    curl -s -X POST "$url" -d "$body" && echo "✓ created" || echo "may already exist"

`curl -s` exits 0 for any COMPLETED transfer. A 400, a 401, a 403 and a 409 are all
success as far as `$?` is concerned — only a transport failure is non-zero. So the
`&&` branch is taken on every outcome.

What that cost, measured on the live project:

* `setup_governance_policies.sh` reported creating `geap-iap-extension` and
  `geap-iap-policy` on every run. Both had existed since 2026-05. It reported creating
  `geap-model-armor-extension` and `geap-model-armor-policy` on every run. Neither
  existed at all.
* `setup_model_armor.sh` reported creating the two Model Armor templates that are the
  entire server-side half of the armor story — the ones `get_armored_generate_config`
  attaches and the console Security tab reads.
* The SGP engine PATCH said "provisioning started" on a rejection, immediately above
  "takes 15-20 minutes to become ACTIVE", so a refused request looked like a slow one.

`gcloud` is deliberately NOT covered: it really does exit non-zero on failure, so
`gcloud … && ok || fail` is correct, and so are `wait $PID && …` and `[ -n "$x" ] && …`.
The bug is specific to commands whose exit status does not carry the outcome.

The fix is `http_send` in `scripts/lib/config.sh`, which reads `%{http_code}`.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
from typing import ClassVar

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = _REPO_ROOT / "scripts"
SHELL_SCRIPTS = sorted(SCRIPTS.glob("*.sh")) + sorted((SCRIPTS / "lib").glob("*.sh"))


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Join backslash continuations, drop comments; keep the starting line number.

    Every real instance of this bug spanned four or five continuation lines with the
    `curl` on the first and the `&& echo` on the last, so a per-physical-line scan
    sees neither half.
    """
    out: list[tuple[int, str]] = []
    buffer = ""
    start = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if line.startswith("#"):
            continue
        if not buffer:
            start = number
        buffer = f"{buffer} {line[:-1].strip()}" if line.endswith("\\") else f"{buffer} {line}"
        if not line.endswith("\\"):
            out.append((start, buffer.strip()))
            buffer = ""
    return out


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_no_curl_asserts_success_from_its_exit_code(script: pathlib.Path) -> None:
    """`curl … && <claim>` is the defect. Find it wherever it reappears."""
    offenders = [
        f"{script.name}:{number}: {line[:110]}"
        for number, line in _logical_lines(script.read_text())
        if re.search(r"\bcurl\b", line) and re.search(r"&&\s*(ok|echo|info|warn)\b", line)
    ]
    assert not offenders, (
        "curl exits 0 on HTTP 4xx/5xx, so these report success on failure. "
        "Use http_send from lib/config.sh instead:\n" + "\n".join(offenders)
    )


def test_the_honest_helper_exists_and_reads_the_status() -> None:
    """The replacement has to actually look at the status code, or this is theatre."""
    lib = (SCRIPTS / "lib" / "config.sh").read_text()
    assert "http_send()" in lib
    assert "%{http_code}" in lib
    assert "HTTP_STATUS=" in lib
    # 409 must stay distinguishable from 2xx: an idempotent create finding its
    # resource present is a success, but a different fact from having created one.
    # Folding them together is how "may already exist" covered for a 401.
    assert "2*|409" in lib, "409 is not handled alongside 2xx as a non-error"


@pytest.mark.parametrize(
    ("script", "claim"),
    [
        ("setup_model_armor.sh", "Model Armor setup complete"),
        ("setup_governance_policies.sh", "policies APPLIED"),
    ],
    ids=["model_armor", "governance"],
)
def test_the_closing_success_line_is_conditional(script: str, claim: str) -> None:
    """A terminal "✓ complete" printed unconditionally is the same lie, louder.

    `setup_model_armor.sh` printed "✓ Model Armor setup complete" directly beneath
    four steps that could each fail silently — two template creates whose curl could
    not fail, and an IAM loop ending in `2>/dev/null || true`.
    """
    text = (SCRIPTS / script).read_text()
    index = text.index(claim)
    preceding = text[:index]
    # The claim must sit inside a conditional, not at the top level of the script.
    assert preceding.rstrip().endswith(("then", "then\n", '"')) or "if [" in preceding[-400:], (
        f"{script}: '{claim}' does not appear to be guarded by a failure count"
    )


def test_model_armor_exits_non_zero_when_a_step_failed() -> None:
    """deploy_all.sh and CI read exit codes, not prose. A run that failed four steps
    used to exit 0."""
    text = (SCRIPTS / "setup_model_armor.sh").read_text()
    assert "MA_FAILURES=0" in text
    assert text.rstrip().endswith('[ "${MA_FAILURES}" -eq 0 ]'), (
        "the script does not end by propagating its own failure count"
    )


def test_no_iam_grant_discards_its_error_and_continues() -> None:
    """`gcloud … 2>/dev/null || true` throws away both the error and the fact that
    there was one. add-iam-policy-binding is idempotent and exits 0 when the binding
    already exists, so a non-zero status is always a real failure."""
    offenders = [
        f"{script.name}:{number}: {line[:110]}"
        for script in SHELL_SCRIPTS
        for number, line in _logical_lines(script.read_text())
        if "add-iam-policy-binding" in line and "|| true" in line
    ]
    assert not offenders, offenders


LIB = (SCRIPTS / "lib" / "config.sh").read_text()


def _extract_fn(name: str) -> str:
    """One shell function, by name, from lib/config.sh."""
    start = LIB.index(f"{name}() {{")
    return LIB[start : LIB.index("\n}\n", start) + len("\n}\n")]


class TestModelArmorGrantNamesTheRightPrincipal:
    """`grant_modelarmor_user` must grant to the ENGINE's identity, not a service agent.

    ADK's ModelArmorPlugin screens from inside the engine, so the caller is the
    engine's AGENT_IDENTITY. `setup_model_armor.sh` granted `roles/modelarmor.user` to
    the Reasoning Engine *service agent* instead — the same wrong-principal mistake
    already paid for once with the Agent Registry grant. Because the plugin defaults
    to `block_on_screening_failure=True`, the result was not weaker screening: a fresh
    Gemini-3 coordinator answered every prompt with ADK's blocked message.

    These execute the REAL function out of lib/config.sh with `gcloud` stubbed, rather
    than asserting on its text — per CODE_STANDARDS "exercise the wiring".
    """

    FUNCTION: ClassVar[str] = _extract_fn("grant_modelarmor_user")

    def _run(self, *, dry_run: str = "false", identity: str = "spiffe-abc") -> str:
        harness = f"""
set -euo pipefail
PROJECT_ID=test-project
DRY_RUN={dry_run}
engine_identity() {{ printf '%s' "{identity}"; }}
gcloud() {{ echo "GCLOUD: $*" >&2; }}
{self.FUNCTION}
grant_modelarmor_user "Coordinator" "12345" || echo "RETURNED_NONZERO"
"""
        res = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=30)
        return res.stdout + res.stderr

    def test_it_grants_to_the_agent_identity(self) -> None:
        out = self._run()
        assert "--member=principal://spiffe-abc" in out, out
        assert "roles/modelarmor.user" in out

    def test_it_never_grants_to_a_service_account(self) -> None:
        """The exact regression: `serviceAccount:service-<N>@gcp-sa-aiplatform-re…`."""
        assert "serviceAccount:" not in self._run()

    def test_a_dry_run_cannot_claim_the_grant(self) -> None:
        """Both of this repo's other grant helpers printed a green success on a dry
        run before being corrected. This one is guarded from the start."""
        out = self._run(dry_run="true")
        assert "[dry-run]" in out
        assert "GCLOUD:" not in out, "a dry run actually invoked gcloud"
        assert "✓" not in out, "a dry run claimed the grant succeeded"

    def test_a_missing_identity_skips_without_failing_the_run(self) -> None:
        """Expected on a fresh install before the engines exist. It must say so and
        return 0, so a bare call under `set -e` does not kill the script."""
        harness = f"""
set -euo pipefail
PROJECT_ID=test-project
engine_identity() {{ return 1; }}
gcloud() {{ echo "GCLOUD: $*" >&2; }}
{self.FUNCTION}
grant_modelarmor_user "Coordinator" "12345"
echo "SURVIVED"
"""
        res = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=30)
        assert "SURVIVED" in res.stdout
        assert "GCLOUD:" not in res.stdout
        assert "skipping Model Armor grant" in res.stderr

    def test_there_is_exactly_one_identity_fetcher(self) -> None:
        """One place turns an engine into a principal. Two would be two chances to
        drift back onto a service agent, which has now happened twice."""
        defs = [f"{f.name}" for f in SHELL_SCRIPTS if "\nengine_identity() {" in f.read_text()]
        assert defs == ["config.sh"], f"engine_identity defined in: {defs}"


class TestAFailedGrantIsNotMissingAccess:
    """`setup_logging_sink.sh` must not call a working pipeline broken.

    The false-success sweep replaced `bq add-iam-policy-binding … || true` with a loud
    failure — correct in principle, wrong here. `bq add-iam-policy-binding` returns
    "This feature requires allowlisting" in this project, so the call ALWAYS fails,
    while the sink's writer already holds WRITER on the dataset through the legacy
    access list and has been writing tables since 2026-08-12.

    So for a few hours the script declared a healthy log pipeline broken, in alarming
    terms, and exited 1. Over-reporting is a different bug from under-reporting, not a
    safe direction: an alarm that is always wrong gets ignored, and then the real one
    is ignored too.
    """

    FUNCTION: ClassVar[str] = (
        lambda text: text[
            text.index("_writer_has_access() {") : text.index(
                "\n}\n", text.index("_writer_has_access() {")
            )
            + 3
        ]
    )((SCRIPTS / "setup_logging_sink.sh").read_text())

    def _check(self, access: list[dict], member: str) -> int:
        """Run the REAL access probe with `bq show` stubbed to a given ACL."""
        import json as _json

        harness = f"""
PROJECT_ID=p
DATASET_NAME=d
bq() {{ cat <<'JSON'
{_json.dumps({"access": access})}
JSON
}}
{self.FUNCTION}
_writer_has_access "{member}"
"""
        return subprocess.run(
            ["bash", "-c", harness], capture_output=True, text=True, timeout=30
        ).returncode

    SA = "serviceAccount:service-1@gcp-sa-logging.iam.gserviceaccount.com"
    EMAIL = "service-1@gcp-sa-logging.iam.gserviceaccount.com"

    def test_legacy_writer_counts_as_access(self) -> None:
        """What the live dataset actually has. A dataset ACL reports the legacy
        spelling, not the IAM role name, so checking only for dataEditor sees nothing."""
        assert self._check([{"role": "WRITER", "userByEmail": self.EMAIL}], self.SA) == 0

    def test_owner_counts_as_access(self) -> None:
        assert self._check([{"role": "OWNER", "userByEmail": self.EMAIL}], self.SA) == 0

    def test_the_iam_role_name_counts_too(self) -> None:
        assert (
            self._check([{"role": "roles/bigquery.dataEditor", "iamMember": self.SA}], self.SA) == 0
        )

    def test_reader_is_not_write_access(self) -> None:
        """The failure must stay real when it IS real — READER cannot write."""
        assert self._check([{"role": "READER", "userByEmail": self.EMAIL}], self.SA) == 1

    def test_a_different_principal_is_not_access(self) -> None:
        assert self._check([{"role": "WRITER", "userByEmail": "someone@else.com"}], self.SA) == 1

    def test_an_empty_acl_is_not_access(self) -> None:
        assert self._check([], self.SA) == 1

    def test_the_script_consults_the_dataset_before_declaring_failure(self) -> None:
        """Structural: the failure branch must be reachable only after the probe."""
        text = (SCRIPTS / "setup_logging_sink.sh").read_text()
        probe = text.index("elif _writer_has_access")
        failure = text.index("has NO write access to")
        assert probe < failure, "the script fails before checking whether access exists"
