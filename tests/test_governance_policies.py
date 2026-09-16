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
import subprocess
import sys
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


def _extract_precheck() -> str:
    """Pull the precheck out of the script by its heredoc delimiter.

    The delimiter is `PRECHECK_PY` and not the `PY` used elsewhere in the file
    precisely so this extraction is unambiguous. A failure here means the block
    moved or was renamed — which is exactly when these tests should stop passing
    rather than quietly testing nothing.
    """
    start = SCRIPT.index("<<'PRECHECK_PY'\n") + len("<<'PRECHECK_PY'\n")
    end = SCRIPT.index("\nPRECHECK_PY\n", start)
    return SCRIPT[start:end]


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

    def _run(self, live: dict, tmp_path: pathlib.Path) -> subprocess.CompletedProcess[str]:
        policy = tmp_path / "policy.json"
        policy.write_text(json.dumps(OURS))
        block = tmp_path / "precheck.py"
        block.write_text(self.PRECHECK)
        return subprocess.run(
            [sys.executable, str(block), str(policy)],
            input=json.dumps(live),
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
        assert res.returncode == 1
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
        assert res.returncode == 1
        assert "principal://someone-elses-engine" in res.stderr

    def test_the_precheck_runs_before_the_write(self) -> None:
        """It reuses the GET that `stamp_policy_etag` already performs, so it costs
        no extra call — but only if it stays ahead of the apply."""
        assert SCRIPT.index("<<'PRECHECK_PY'") < SCRIPT.index("apply_iap_policy()")


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
