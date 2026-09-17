"""Offline tests for the consolidated pre-demo readiness check (no live GCP).

Every underlying ``verify_*`` call is injected, so the compose → render → gate
path is exercised without any network, credentials, or engine calls.
"""

from src.deploy.engine_baseline import Finding
from src.eval.demo_readiness import (
    build_default_checks,
    check_engine_config,
    check_engine_live,
    check_gateway_callouts,
    check_mcp_tools,
    check_memory,
    check_monitors,
    check_recall,
    is_ready,
    main,
    render,
    run_readiness,
)


def _finding(name, ok, severity):
    return Finding(
        name=name, ok=ok, severity=severity, expected="x", observed="y", why="because " * 8
    )


# --------------------------------------------------------------------------- #
# Individual checks (each returns (ok, detail))
# --------------------------------------------------------------------------- #
class TestChecks:
    def test_mcp_tools_pass_and_fail(self):
        ok, detail = check_mcp_tools(run_checks_fn=lambda: [{"ok": True}, {"ok": True}])
        assert ok is True
        assert "2/2" in detail
        ok, _ = check_mcp_tools(run_checks_fn=lambda: [{"ok": True}, {"ok": False}])
        assert ok is False

    def test_mcp_tools_empty_is_fail(self):
        ok, _ = check_mcp_tools(run_checks_fn=list)
        assert ok is False

    def test_monitors_status(self):
        ok, detail = check_monitors(verify_fn=lambda **_: {"status": "ok"})
        assert ok is True
        assert "ok" in detail
        ok, _ = check_monitors(verify_fn=lambda **_: {"status": "degraded"})
        assert ok is False

    def test_engine_config_clean_passes(self):
        ok, detail = check_engine_config(
            engine_id="e",
            check_fn=lambda _e: {"findings": [_finding("memory", True, "critical")]},
        )
        assert ok is True
        assert "0 critical" in detail

    def test_engine_config_critical_drift_fails_and_names_the_check(self):
        ok, detail = check_engine_config(
            engine_id="e",
            check_fn=lambda _e: {"findings": [_finding("memory", False, "critical")]},
        )
        assert ok is False
        assert "memory" in detail

    def test_engine_config_advisory_alone_still_passes(self):
        """Advisories are posture, not breakage — they must not block a demo."""
        ok, detail = check_engine_config(
            engine_id="e",
            check_fn=lambda _e: {"findings": [_finding("min_instances", False, "advisory")]},
        )
        assert ok is True
        assert "1 advisory" in detail

    def test_engine_config_unreachable_is_a_failure(self):
        ok, detail = check_engine_config(
            engine_id="e", check_fn=lambda _e: {"error": "403 denied", "findings": []}
        )
        assert ok is False
        assert "unreachable" in detail

    def test_engine_config_is_a_default_critical_check(self):
        """Wiring it in is the whole point — a verifier nobody runs helps nobody."""
        names = {c["name"]: c for c in build_default_checks(engine_id="e", user_id="alice")}
        assert "engine_config" in names
        assert names["engine_config"]["critical"] is True

    def test_memory_presence(self):
        ok, detail = check_memory(
            engine_id="e", user_id="alice", fetch_fn=lambda *a, **k: ["fact1", "fact2"]
        )
        assert ok is True
        assert "2" in detail
        ok, _ = check_memory(engine_id="e", user_id="alice", fetch_fn=lambda *a, **k: [])
        assert ok is False

    def test_engine_live_retries_until_nonempty(self):
        # capture_fn returns [(prompt, response), ...]; retry past cold-start empties.
        replies = iter([[("p", "")], [("p", "  ")], [("p", "hello there")]])
        ok, detail = check_engine_live(
            engine_id="e", engine=object(), capture_fn=lambda *a, **k: next(replies), attempts=3
        )
        assert ok is True
        assert "attempt 3/3" in detail

    def test_engine_live_all_empty_fails(self):
        ok, detail = check_engine_live(
            engine_id="e", engine=object(), capture_fn=lambda *a, **k: [("p", "")], attempts=2
        )
        assert ok is False
        assert "empty-at-200" in detail

    def test_engine_live_retries_past_raised_error(self):
        # A raised error-shaped stream on attempt 1 must be retried, not fatal.
        calls = {"n": 0}

        def flaky(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("Can only parse array of JSON objects, instead got {")
            return [("p", "warmed up now")]

        ok, detail = check_engine_live(engine_id="e", engine=object(), capture_fn=flaky, attempts=3)
        assert ok is True
        assert "attempt 2/3" in detail

    def test_engine_live_all_errored_reports_last_error(self):
        def boom(*_a, **_k):
            raise ValueError("stream boom")

        ok, detail = check_engine_live(engine_id="e", engine=object(), capture_fn=boom, attempts=2)
        assert ok is False
        assert "last error" in detail
        assert "stream boom" in detail

    def test_recall(self):
        ok, _ = check_recall(
            engine_id="e", user_id="alice", recall_fn=lambda *a, **k: {"recalled": True}
        )
        assert ok is True
        ok, _ = check_recall(
            engine_id="e", user_id="alice", recall_fn=lambda *a, **k: {"recalled": False}
        )
        assert ok is False


# --------------------------------------------------------------------------- #
# Composition / gate
# --------------------------------------------------------------------------- #
def _check(name, ok, critical=True):
    return {"name": name, "critical": critical, "run": lambda: (ok, f"{name} detail")}


class TestRunReadiness:
    def test_runs_all_and_records_results(self):
        checks = [_check("a", True), _check("b", False)]
        results = run_readiness(checks=checks)
        assert [r["name"] for r in results] == ["a", "b"]
        assert [r["ok"] for r in results] == [True, False]

    def test_exception_becomes_red_row(self):
        def boom():
            raise RuntimeError("kaboom")

        results = run_readiness(checks=[{"name": "x", "critical": True, "run": boom}])
        assert results[0]["ok"] is False
        assert "kaboom" in results[0]["detail"]

    def test_is_ready_ignores_noncritical_failures(self):
        results = run_readiness(checks=[_check("a", True), _check("b", False, critical=False)])
        assert is_ready(results) is True

    def test_is_ready_false_on_critical_failure(self):
        results = run_readiness(checks=[_check("a", True), _check("b", False, critical=True)])
        assert is_ready(results) is False

    def test_render_shows_pass_fail(self):
        out = render(run_readiness(checks=[_check("a", True), _check("b", False)]))
        assert "PASS" in out
        assert "FAIL" in out
        assert "a" in out and "b" in out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
class TestMain:
    def test_exit_zero_when_ready(self, capsys):
        rc = main(["--engine-id", "e"], checks=[_check("a", True)])
        assert rc == 0
        assert "PASS" in capsys.readouterr().out

    def test_exit_one_when_critical_fails(self):
        rc = main(["--engine-id", "e"], checks=[_check("a", False, critical=True)])
        assert rc == 1

    def test_json_output(self, capsys):
        rc = main(["--engine-id", "e", "--json"], checks=[_check("a", True)])
        assert rc == 0
        out = capsys.readouterr().out
        assert '"ok": true' in out
        assert '"name": "a"' in out


class TestTheGatewayCalloutRow:
    """The green board must show fail-open, without crying wolf about `no_data`.

    Both authz extensions are `failOpen: true`: a failed callout lets the request
    through unevaluated. Google emits `extension/failed_open_count` for it, and until
    now nothing on the readiness board read it.

    The tension this row resolves: nothing is attached to a gateway, so the series is
    permanently empty. A red row on every run is the `agent_router/*` mistake — an
    alarm that is always wrong gets ignored. A silently green row is the
    `server_side_armor` mistake — absence read as health. So the row is green and the
    DETAIL carries the verdict verbatim.
    """

    def test_an_observed_fail_open_is_a_red_row(self) -> None:
        ok, detail = check_gateway_callouts(
            read_fn=lambda _h: {"verdict": "failing", "detail": "2 of 100 callouts FAILED OPEN"}
        )
        assert ok is False
        assert "failing" in detail

    def test_no_data_is_green_but_says_so(self) -> None:
        """Green, because it is the expected state — but the detail must make it
        impossible to read as 'verified working'."""
        ok, detail = check_gateway_callouts(
            read_fn=lambda _h: {"verdict": "no_data", "detail": "UNOBSERVED, not healthy."}
        )
        assert ok is True
        assert "no_data" in detail
        assert "UNOBSERVED" in detail

    def test_real_traffic_with_no_failures_is_green(self) -> None:
        ok, detail = check_gateway_callouts(
            read_fn=lambda _h: {"verdict": "ok", "detail": "120 callouts, none failed open."}
        )
        assert ok is True
        assert "ok" in detail

    def test_it_is_advisory_and_never_gates_the_demo(self) -> None:
        """A fail-open is a governance problem, not a reason to block a working demo —
        and `is_ready` only considers critical rows."""
        checks = build_default_checks(engine_id="1", user_id="alice")
        row = next(c for c in checks if c["name"] == "gateway_callouts")
        assert row["critical"] is False
        results = [{"name": "gateway_callouts", "ok": False, "critical": False, "detail": "x"}]
        assert is_ready(results) is True

    def test_it_is_on_the_default_board(self) -> None:
        """A verifier nothing calls is the same failure as a metric nobody watches."""
        names = [c["name"] for c in build_default_checks(engine_id="1", user_id="alice")]
        assert "gateway_callouts" in names

    def test_a_broken_reader_renders_red_rather_than_crashing_the_board(self) -> None:
        def boom(_h):
            raise RuntimeError("monitoring unreachable")

        results = run_readiness(
            checks=[
                {
                    "name": "gateway_callouts",
                    "critical": False,
                    "run": lambda: check_gateway_callouts(read_fn=boom),
                }
            ]
        )
        assert results[0]["ok"] is False
        assert "RuntimeError" in results[0]["detail"]
