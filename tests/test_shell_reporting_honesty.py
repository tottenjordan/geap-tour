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
