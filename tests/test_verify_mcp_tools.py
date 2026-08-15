"""``verify_mcp_tools`` turns silent tool-less MCP toolsets into a loud failure.

The pure check logic (`evaluate_toolset`, `run_checks`, `main`) is exercised with
fakes — no GCP, no MCP connections.
"""

import src.eval.verify_mcp_tools as v

# Distinct server names so a fake enumerator can key off them without depending
# on the (env-derived, often empty) real SERVER_NAMES values.
_NAMES = {"search": "S", "booking": "B", "expense": "E"}


def _good(name):
    """A fake enumerator that returns the full expected tool set for each server."""
    domain = next(d for d, n in _NAMES.items() if n == name)
    return sorted(v.EXPECTED_TOOLS[domain])


def test_evaluate_toolset_all_present():
    r = v.evaluate_toolset("search", ["search_flights", "search_hotels"])
    assert r["ok"] is True
    assert r["missing"] == []


def test_evaluate_toolset_missing_one_is_failure():
    r = v.evaluate_toolset("expense", ["submit_expense"])
    assert r["ok"] is False
    assert "check_expense_policy" in r["missing"]
    assert "get_user_expenses" in r["missing"]


def test_evaluate_toolset_empty_is_failure():
    r = v.evaluate_toolset("booking", [])
    assert r["ok"] is False
    assert r["resolved"] == []


def test_evaluate_toolset_extra_tools_still_ok():
    r = v.evaluate_toolset("search", ["search_flights", "search_hotels", "bonus"])
    assert r["ok"] is True


def test_run_checks_all_ok():
    results = v.run_checks(server_names=_NAMES, enumerate_fn=_good)
    assert [r["domain"] for r in results] == ["search", "booking", "expense"]
    assert all(r["ok"] for r in results)


def test_run_checks_flags_toolless_domain():
    def enum(name):
        return [] if name == _NAMES["expense"] else _good(name)

    results = v.run_checks(server_names=_NAMES, enumerate_fn=enum)
    by_domain = {r["domain"]: r for r in results}
    assert by_domain["search"]["ok"] is True
    assert by_domain["expense"]["ok"] is False


def test_run_checks_captures_enumeration_error():
    def enum(name):
        if name == _NAMES["search"]:
            raise RuntimeError("Session terminated")
        return _good(name)

    results = v.run_checks(server_names=_NAMES, enumerate_fn=enum)
    search = next(r for r in results if r["domain"] == "search")
    assert search["ok"] is False
    assert "Session terminated" in search["error"]


def test_run_checks_flags_unconfigured_server_name():
    """An empty server name (env var unset) is a FAIL, not a crash."""
    results = v.run_checks(
        server_names={"search": "", "booking": "B", "expense": "E"},
        enumerate_fn=_good,
    )
    search = next(r for r in results if r["domain"] == "search")
    assert search["ok"] is False
    assert "SEARCH_MCP_SERVER" in search["error"]


def test_main_exits_zero_when_all_pass(capsys):
    rc = v.main([], server_names=_NAMES, enumerate_fn=_good)
    assert rc == 0
    out = capsys.readouterr().out
    assert "PASS" in out
    assert "FAIL" not in out


def test_main_exits_nonzero_on_any_failure(capsys):
    def enum(name):
        return [] if name == _NAMES["booking"] else _good(name)

    rc = v.main([], server_names=_NAMES, enumerate_fn=enum)
    assert rc == 1
    assert "FAIL" in capsys.readouterr().out


def test_main_json_output_is_parseable(capsys):
    import json

    rc = v.main(["--json"], server_names=_NAMES, enumerate_fn=_good)
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert {r["domain"] for r in parsed} == {"search", "booking", "expense"}
