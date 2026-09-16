"""Guards for the defects Layer 1 of `setup_governance_policies.sh` shipped with.

Layer 1 wrote three IAM policy files to `/tmp`, printed `IAM policy created` three
times, and applied none of them. When that was corrected, the audit found the
policies it would now really apply were wrong in four independent ways at once:

* the only `set-iam-policy` in an 800-line script was inside an `info` string
  telling the operator to run it by hand;
* the principal was `principal://${RE_SA}` — the Reasoning Engine *service agent*,
  in SPIFFE syntax that fits neither a SPIFFE id nor a service account;
* the conditions read `mcp.tool.isReadOnly` / `isDestructive` from tools declared
  as bare `@mcp.tool()`, so with `getAttribute(..., false)` defaults `isReadOnly ==
  true` never matched (denying ALL search) and `isDestructive == false` always did
  (constraining nothing);
* the allowlist named `get_expenses`, a tool that does not exist, and assumed a
  Coordinator/Travel/Expense topology removed on 2026-08-20.

Two more surfaced in pre-execution review, and only because the apply had landed:
Step 0 attached the gateway on any non-dry run (turning on enforcement unattended),
and `set-iam-policy` replaces a resource's WHOLE policy, so a binding committed by
someone else on this shared project would be deleted with a valid etag and an
`applied` line.

Nothing executes this shell in CI, so most of these are text assertions — weak, but
they are what would have caught the original four. The foreign-binding precheck is
the exception: it is real Python embedded in the script, so it is extracted and
actually exercised below.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
from typing import ClassVar

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = _REPO_ROOT / "scripts" / "setup_governance_policies.sh"
SCRIPT = SCRIPT_PATH.read_text()


class TestLayer1AppliesAndTargetsRealThings:
    """One assertion per original defect."""

    def test_it_applies_rather_than_printing_the_command(self) -> None:
        assert "apply_iap_policy()" in SCRIPT, "the apply function is gone"
        assert "gcloud beta iap web set-iam-policy" in SCRIPT

    def test_it_uses_the_real_gcloud_flag(self) -> None:
        """`--mcpServer` is not a flag. The real one is `--mcp-server`; the script
        printed the camelCase form for months, so anyone who copied the printed
        command got an error rather than a policy."""
        assert "--mcpServer" not in SCRIPT
        assert "--mcp-server=" in SCRIPT

    def test_it_never_binds_the_reasoning_engine_service_agent(self) -> None:
        """Egress IAM evaluates against the AGENT identity. A role on the RE service
        agent buys nothing — CLAUDE.md documents this as the wrong-principal
        mistake, found once already in `grant_registry_read`."""
        assert "principal://${RE_SA}" not in SCRIPT

    async def test_the_expense_allowlist_names_a_tool_that_exists(self) -> None:
        from src.mcp_servers.expense import server

        real = {t.name for t in await server.mcp.list_tools()}
        assert "get_user_expenses" in real, "test is stale, not the script"
        assert "get_expenses" not in real
        assert "'get_expenses'" not in SCRIPT, "allowlists a tool that does not exist"
        assert "get_user_expenses" in SCRIPT

    def test_an_empty_mcp_server_id_is_refused(self) -> None:
        """An empty `--mcp-server` is not a narrower target, it is a much wider one:
        gcloud's ParseIapIamResource falls through to the WHOLE agent registry. So
        an unset SEARCH_MCP_SERVER would not skip a server, it would overwrite the
        registry's own policy."""
        assert "refusing to apply" in SCRIPT
        assert "retarget this policy at the whole agent registry" in SCRIPT


class TestAttachingTheGatewayRequiresTheFlag:
    """Step 0 PATCHes `agentGatewayConfig` onto the live coordinator and router.

    It used to be gated on `! $DRY_RUN` alone. That was inert while Layer 1 only
    wrote files; once the apply landed, any real run attached the gateway and then
    applied deny-by-default egress policies to the engines it had just attached —
    enforcement, switched on unattended, by a script whose own output calls itself
    audit-only.
    """

    def test_the_attach_is_gated_on_enable_agent_gateway(self) -> None:
        assert "GW_REQUESTED" in SCRIPT, "the gateway flag gate is gone"
        gate = SCRIPT.index('case "$ENABLE_AGENT_GATEWAY"')
        first_attach = SCRIPT.index('attach_gateway "Coordinator"')
        assert gate < first_attach, "the flag is parsed after the attach it must gate"

    def test_the_flag_default_is_off(self) -> None:
        assert 'ENABLE_AGENT_GATEWAY="${ENABLE_AGENT_GATEWAY:-0}"' in SCRIPT

    def test_dry_run_alone_no_longer_decides(self) -> None:
        """The precise regression: `if ! $DRY_RUN; then <attach>`. If that shape
        returns, a dry run is again the only thing standing between a routine
        invocation and enforced egress on two production engines."""
        attach_block = SCRIPT[
            SCRIPT.index('step "Step 0: Agent-to-Gateway Attachment"') : SCRIPT.index(
                'attach_gateway "Coordinator"'
            )
        ]
        # Compared line-by-line, stripped: `elif ! $DRY_RUN; then` — the correct
        # shape, second in the chain — CONTAINS the regressed shape as a substring,
        # so a plain `in` check here passes on exactly the code it exists to reject.
        opener = [
            line.strip() for line in attach_block.splitlines() if line.strip().startswith("if ")
        ]
        assert "if ! $GW_REQUESTED; then" in opener, opener
        assert "if ! $DRY_RUN; then" not in opener, "the flag no longer gates the attach"


# ---------------------------------------------------------------------------
# The foreign-binding precheck, extracted and actually run.
# ---------------------------------------------------------------------------


def _precheck_heredoc_line() -> int:
    """Index of the line that really opens the precheck heredoc.

    Located line-by-line rather than by a substring anchor, because both obvious
    anchors have already broken once each:

    * `index("<<'PRECHECK_PY'")` also matches the COMMENT above the invocation, which
      quotes the old broken form verbatim — extraction then silently returned the
      one-argument version being described as wrong;
    * `index("<<'PRECHECK_PY'\\n")` dodged the comment by requiring a line break, then
      broke the moment the real line grew a trailing `|| precheck_rc=$?`.

    Skipping comments and ignoring whatever follows the operator is stable against
    both. The delimiter is `PRECHECK_PY`, distinct from the `PY` used elsewhere in
    the file, so exactly one non-comment line can match.
    """
    matches = [
        i
        for i, line in enumerate(SCRIPT.splitlines())
        if "<<'PRECHECK_PY'" in line and not line.strip().startswith("#")
    ]
    assert len(matches) == 1, f"expected one precheck heredoc, found {len(matches)}"
    return matches[0]


def _extract_precheck() -> str:
    """The Python program the heredoc supplies."""
    lines = SCRIPT.splitlines()
    start = _precheck_heredoc_line() + 1
    end = lines.index("PRECHECK_PY", start)
    return "\n".join(lines[start:end])


def _extract_invocation() -> str:
    """The `python3 - …` command the script really runs, minus the heredoc operator.

    This is the part the first version of these tests threw away, and it was where
    the bug lived. See `TestTheApplyRefusesToDeleteSomeoneElsesBinding._run`.
    """
    line = SCRIPT.splitlines()[_precheck_heredoc_line()]
    return line[: line.index("<<'PRECHECK_PY'")].strip()


def _extract_dispatch() -> str:
    """The bash that captures the precheck's exit code and decides what to do with it.

    Extracted as TEXT and executed, because the bug this guards against lived here
    and not in the Python: the tests drove the comparison directly, so the wrapper
    around it was never executed by anything but a live run.
    """
    start = SCRIPT.index("    precheck_rc=0\n")
    marker = SCRIPT.index("the precheck itself FAILED", start)
    return SCRIPT[start : SCRIPT.index("\n    fi\n", marker) + len("\n    fi\n")]


OURS = {
    "version": 3,
    "bindings": [
        {
            "role": "roles/iap.egressor",
            "members": ["principal://coordinator-id", "principal://router-id"],
            "condition": {"title": "t", "description": "d", "expression": "CURRENT_CEL"},
        }
    ],
}


class TestTheApplyRefusesToDeleteSomeoneElsesBinding:
    """`set-iam-policy` REPLACES a resource's whole policy.

    The etag guards the window between our read and our write. It does nothing
    about a binding committed by someone else days earlier: that is dropped with a
    valid etag, no conflict, and an `applied` line. The etag makes the deletion
    silent, not impossible — and this is a shared project.
    """

    PRECHECK: ClassVar[str] = _extract_precheck()
    INVOCATION: ClassVar[str] = _extract_invocation()

    def _run(self, live: dict, tmp_path: pathlib.Path) -> subprocess.CompletedProcess[str]:
        """Drive the precheck THE WAY THE SCRIPT DOES: through bash, as a heredoc.

        The first version of this helper wrote the extracted block to a file and ran
        `python3 block.py policy.json` with the live policy piped to stdin. Every
        case passed. The script meanwhile ran

            printf '%s' "$current" | python3 - "$file" <<'PRECHECK_PY'

        where `python3 -` reads the PROGRAM from stdin and the heredoc supplies it —
        so stdin was double-booked, `json.load(sys.stdin)` got the empty remainder,
        and the precheck died before comparing anything. It failed closed, so nothing
        was destroyed, but it refused all three servers with a specific and entirely
        wrong diagnosis, and the suite was green throughout.

        A harness that reconstructs the invocation cannot see a bug IN the invocation.
        So this reads the real `python3 -` line out of the script and runs the real
        heredoc, and the only thing it supplies is the two policy files.
        """
        policy = tmp_path / "policy.json"
        policy.write_text(json.dumps(OURS))
        live_file = tmp_path / "policy.json.live"
        live_file.write_text(json.dumps(live))

        invocation = self.INVOCATION.replace('"${file}"', str(policy)).replace(
            '"${live_file}"', str(live_file)
        )
        assert "${" not in invocation, f"unsubstituted variable in: {invocation}"

        return subprocess.run(
            ["bash", "-c", f"{invocation} <<'PRECHECK_PY'\n{self.PRECHECK}\nPRECHECK_PY\n"],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_an_empty_live_policy_is_allowed(self, tmp_path: pathlib.Path) -> None:
        """All three MCP servers are in this state today (`etag: ACAB`, no
        bindings). If the precheck blocked here it would block the first real
        apply, which is the whole point of the exercise."""
        assert self._run({"etag": "ACAB"}, tmp_path).returncode == 0

    def test_rerunning_after_a_cel_edit_is_allowed(self, tmp_path: pathlib.Path) -> None:
        """Conditions are deliberately NOT part of the identity key. Including them
        would make every CEL change look like a foreign binding and render the
        script un-rerunnable after any policy edit — which would get the precheck
        deleted rather than fixed."""
        live = {
            "etag": "X",
            "bindings": [
                {
                    "role": "roles/iap.egressor",
                    "members": ["principal://coordinator-id", "principal://router-id"],
                    "condition": {"expression": "AN_OLDER_DIFFERENT_CEL"},
                }
            ],
        }
        assert self._run(live, tmp_path).returncode == 0

    def test_a_foreign_role_aborts(self, tmp_path: pathlib.Path) -> None:
        live = {
            "etag": "X",
            "bindings": [
                {"role": "roles/iap.admin", "members": ["user:someone@example.com"]},
            ],
        }
        res = self._run(live, tmp_path)
        assert res.returncode == 3, f"expected the foreign-binding code, got {res.returncode}"
        assert "roles/iap.admin -> user:someone@example.com" in res.stderr

    def test_an_extra_member_on_our_own_role_aborts(self, tmp_path: pathlib.Path) -> None:
        """The subtle one: same role, same condition, one more principal. A plain
        replace drops that third engine's egress with no diagnostic at all."""
        live = {
            "etag": "X",
            "bindings": [
                {
                    "role": "roles/iap.egressor",
                    "members": [
                        "principal://coordinator-id",
                        "principal://router-id",
                        "principal://someone-elses-engine",
                    ],
                }
            ],
        }
        res = self._run(live, tmp_path)
        assert res.returncode == 3, f"expected the foreign-binding code, got {res.returncode}"
        assert "principal://someone-elses-engine" in res.stderr

    DISPATCH: ClassVar[str] = _extract_dispatch()

    def _dispatch(self, stub_rc: int, tmp_path: pathlib.Path) -> str:
        """Run the real capture-and-dispatch bash with `python3` stubbed to exit N.

        Returns APPLIED / REFUSED-FOREIGN / REFUSED-CRASH.
        """
        harness = f"""
python3() {{ cat >/dev/null; return {stub_rc}; }}
fail() {{ echo "FAIL: $*"; }}
file="{tmp_path}/p.json"
live_file="{tmp_path}/p.json.live"
: > "$live_file"
check() {{
{self.DISPATCH}
  return 0
}}
if check; then echo RESULT=APPLIED; fi
"""
        res = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=30)
        if "RESULT=APPLIED" in res.stdout:
            return "APPLIED"
        if "did not author" in res.stdout:
            return "REFUSED-FOREIGN"
        return "REFUSED-CRASH"

    @pytest.mark.parametrize(
        ("exit_code", "expected"),
        [(0, "APPLIED"), (3, "REFUSED-FOREIGN"), (1, "REFUSED-CRASH"), (2, "REFUSED-CRASH")],
    )
    def test_the_exit_code_reaches_the_right_branch(
        self, exit_code: int, expected: str, tmp_path: pathlib.Path
    ) -> None:
        """The wrapper was written `if ! cmd; then rc=0; else rc=$?; fi`, which
        INVERTS the guard.

        `!` negates the status, so the `else` branch runs when the command SUCCEEDED
        and `$?` there is the negation's own 1 — never the command's code. Measured
        against the shipped text:

            real exit 0  ->  rc=1   clean policy reported as a crash
            real exit 3  ->  rc=0   FOREIGN BINDING REPORTED AS CLEAN
            real exit 1  ->  rc=0   crash reported as clean

        The middle row is the guard turned into its opposite: the one case it exists
        to catch would have been applied, deleting someone else's binding. It only
        ever failed safe because all three live policies are empty, so the real code
        was 0 and the inversion happened to map that to a refusal.

        The earlier tests drove the Python comparison directly and could not see any
        of this — the bash around it was executed only by a live run against the
        shared project.
        """
        assert self._dispatch(exit_code, tmp_path) == expected

    def test_no_heredoc_program_also_reads_stdin(self) -> None:
        """`python3 -` takes its PROGRAM from stdin, so a heredoc-fed program has no
        stdin left to read data from.

        This is the bug in the class's own docstring, stated structurally so it cannot
        come back in another block: any program supplied by `<<'…'` must take its
        input from argv. `python3 -c` is unaffected — the program is an argument
        there, so stdin stays free, which is why the etag extraction a few lines below
        the precheck can and does pipe into it.
        """
        lines = SCRIPT.splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith("#"):
                continue
            found = re.search(r"python3 - .*?<<'(\w+)'", line)
            if not found:
                continue
            delimiter = found.group(1)
            body = "\n".join(lines[i + 1 : lines.index(delimiter, i + 1)])
            assert "sys.stdin" not in body, (
                f"the {delimiter} heredoc reads sys.stdin, but its own program came "
                f"from there — it will see an empty stream"
            )

    def test_a_crash_is_not_reported_as_a_foreign_binding(self) -> None:
        """The stdin bug printed 'the live policy holds binding(s) this script did
        not author' three times, for three empty policies, because any non-zero exit
        took that branch — and an uncaught Python exception also exits 1.

        The comparison now exits 3 when it finds something, so the caller can tell
        'I compared and found a foreign binding' from 'I never got as far as
        comparing'. Both still refuse to apply.
        """
        assert "sys.exit(3 if foreign else 0)" in SCRIPT
        assert 'if [ "${precheck_rc}" -eq 3 ]; then' in SCRIPT
        assert "the precheck itself FAILED" in SCRIPT

    def test_the_precheck_runs_before_the_write(self) -> None:
        """It reuses the GET that `stamp_policy_etag` already performs, so it costs
        no extra call — but only if it stays ahead of the apply."""
        assert SCRIPT.index("<<'PRECHECK_PY'") < SCRIPT.index("apply_iap_policy()")


class TestLayer3IsOptInAndReportsHonestly:
    """Layer 3 ran on EVERY invocation, and reported success whatever happened.

    A bare run is advertised by the script's own usage text as "IAM Allow policies
    only". It also created two authz extensions and two authz policies on the ingress
    gateway, and granted two roles to the gateway service account at PROJECT level on
    a shared project. Two of those four resources did not exist while the script
    claimed for months to be creating them — because `curl -s` exits 0 for any
    completed transfer, so `curl … && ok "created" || warn "may already exist"` took
    the `ok` branch on a 401 as readily as on a 200.
    """

    def test_layer3_is_gated(self) -> None:
        assert "ENABLE_LAYER3=false" in SCRIPT, "the default must be off"
        assert "--layer3) ENABLE_LAYER3=true" in SCRIPT
        gate = SCRIPT.index("if ! $ENABLE_LAYER3; then")
        first_post = SCRIPT.index('l3_post "IAP authz extension"')
        assert gate < first_post, "the flag is tested after the POST it must gate"

    def test_no_create_reports_success_off_a_bare_curl_exit_code(self) -> None:
        """The exact regressed shape, in the layers that POST to REST endpoints.

        `gcloud` is excluded deliberately: it *does* exit non-zero on failure, so
        `&& ok || fail` is sound for the VPC/subnet/DNS creates in Layer 2.
        """
        offenders = [
            line.strip()
            for line in SCRIPT.splitlines()
            if "&& ok " in line and "curl" in line and not line.strip().startswith("#")
        ]
        assert not offenders, offenders

    def test_every_rest_create_goes_through_the_honest_helper(self) -> None:
        """A raw `run_cmd curl -s -X POST` is how the false success gets back in.

        Comment lines are skipped: `post_resource`'s docstring quotes the old shape
        verbatim, and that quotation is the record of why the helper exists.
        """
        raw = [
            line.strip()
            for line in SCRIPT.splitlines()
            if "run_cmd curl -s -X POST" in line and not line.strip().startswith("#")
        ]
        assert not raw, raw
        for label in ("IAP authz extension", "Model Armor authz extension"):
            assert f'l3_post "{label}"' in SCRIPT
        for label in ("SGP authz extension", "SGP authz policy"):
            assert f'post_resource "{label}"' in SCRIPT

    def test_409_is_distinct_from_created_and_from_failure(self) -> None:
        """An idempotent create finding its resource present is a success, but it is
        a different fact from having created one. Collapsing the two back into "may
        already exist" is exactly how a 401 got to look like a success."""
        helper = SCRIPT[SCRIPT.index("post_resource() {") : SCRIPT.index("l3_post() {")]
        assert 'POST_RESULT="created"' in helper
        assert 'POST_RESULT="exists"' in helper
        assert 'POST_RESULT="failed"' in helper
        assert "409)" in helper

    def test_the_summary_cannot_claim_resources_it_did_not_touch(self) -> None:
        """It used to print all four names as a flat list — on a skipped run, on a dry
        run, and on a run where every create returned 401."""
        summary = SCRIPT[SCRIPT.index("GEAP Governance Policy Summary") :]
        assert "Layer 3 — Authorization Delegation (SKIPPED" in summary
        assert "[dry-run] nothing created" in summary
        assert "${L3_CREATED} created" in summary

    def test_the_step0_summary_tracks_the_gateway_flag(self) -> None:
        """Both branches predated the ENABLE_AGENT_GATEWAY gate and outlived it by a
        commit: a dry run announced "Would attach 2 agents" while the flag was off,
        and a skipped run blamed "private preview enrollment" for a skip the flag
        had caused — sending the reader to check an enrollment that is not the
        reason."""
        summary = SCRIPT[SCRIPT.index("GEAP Governance Policy Summary") :]
        step0 = summary[summary.index("Step 0 — Gateway Attachment") :]
        step0 = step0[: step0.index("Layer 1 —")]
        assert "if ! $GW_REQUESTED; then" in step0
        assert "private preview enrollment" not in step0

    @pytest.mark.parametrize(
        ("function", "claim"),
        [
            ("grant_gateway_sa_role", 'ok "${role} granted'),
            ("grant_registry_read", 'ok "${label}: agentregistry.viewer granted'),
        ],
    )
    def test_a_dry_run_never_claims_a_grant(self, function: str, claim: str) -> None:
        """`ok "… granted"` is a claim, so a dry run must not reach it.

        Both IAM grant helpers had the same shape and both got it wrong: the
        `>/dev/null` that hides add-iam-policy-binding's policy dump also swallows
        `run_cmd`'s own `[dry-run] …` line, so a dry run printed a green success and
        did not even echo the command it had skipped.

        Parametrized because fixing one and missing the other is exactly what
        happened — `grant_gateway_sa_role` was corrected while writing the Layer 3
        gate, and `grant_registry_read`, forty lines up, kept the bug until a dry run
        against the live project printed "agentregistry.viewer granted" twice for
        grants it had not performed.
        """
        fn = SCRIPT[SCRIPT.index(f"{function}() {{") :]
        fn = fn[: fn.index("\n}\n")]
        assert claim in fn, "test is stale — the success line was renamed"
        assert "if $DRY_RUN; then" in fn, f"{function} has no dry-run guard at all"
        assert fn.index("if $DRY_RUN; then") < fn.index(claim), (
            f"{function} can reach its success claim on a dry run"
        )

    def test_no_command_hides_its_dry_run_echo_behind_devnull(self) -> None:
        """The structural form of the bug above, so a THIRD helper cannot reintroduce
        it: `run_cmd <cmd> >/dev/null` discards the very line run_cmd exists to print.

        Only BACKSLASH CONTINUATIONS are joined — not the whole file. Both real
        instances spanned four lines with `run_cmd` on the first and `>/dev/null` on
        the last, so a per-physical-line check misses them; but flattening everything
        into one string runs past command boundaries and flagged
        `result=$(run_cmd "$@" 2>&1)` in create_sgp_policy, which is correct code that
        captures output in order to parse it.

        `2>/dev/null` is excluded for the same reason: discarding stderr is not this
        bug. Only the command's own stdout carries run_cmd's dry-run echo.
        """
        logical: list[str] = []
        buffer = ""
        for raw in SCRIPT.splitlines():
            line = raw.strip()
            if line.startswith("#"):
                continue
            buffer = f"{buffer} {line[:-1].strip()}" if line.endswith("\\") else f"{buffer} {line}"
            if not line.endswith("\\"):
                logical.append(buffer.strip())
                buffer = ""

        offenders = [
            line[:140]
            for line in logical
            # `$(run_cmd …)` captures deliberately; a bare `run_cmd … >/dev/null` throws
            # the echo away. `(?<![0-9&])` keeps `2>/dev/null` and `&>/dev/null` out.
            if "run_cmd " in line
            and "$(run_cmd" not in line
            and re.search(r"(?<![0-9&])>/dev/null", line)
        ]
        assert not offenders, offenders


class TestTheScriptStillParses:
    """Cheap, and the only check here that covers the 800 lines these tests do not."""

    @pytest.mark.parametrize(
        "script",
        ["setup_governance_policies.sh", "deploy_all.sh"],
    )
    def test_bash_accepts_it(self, script: str) -> None:
        res = subprocess.run(
            ["bash", "-n", str(_REPO_ROOT / "scripts" / script)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res.returncode == 0, res.stderr
