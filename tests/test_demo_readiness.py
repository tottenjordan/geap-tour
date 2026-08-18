"""Offline tests for the consolidated pre-demo readiness check (no live GCP).

Every underlying ``verify_*`` call is injected, so the compose → render → gate
path is exercised without any network, credentials, or engine calls.
"""

from src.eval.demo_readiness import (
    check_engine_live,
    check_mcp_tools,
    check_memory,
    check_monitors,
    check_recall,
    is_ready,
    main,
    render,
    run_readiness,
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
