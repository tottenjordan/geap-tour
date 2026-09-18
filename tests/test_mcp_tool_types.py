"""The MCP tool payload shapes, and the two constraints that make them unusual.

**1. The consumer is a model, not Python.** Nothing in this repo reads
`result["truncated"]` in code — the coordinator reads it because
`list_all_bookings`' docstring tells it to. So the usual backstop, "a consumer
would crash", does not exist: a tool that quietly stops emitting `truncated`
produces no error anywhere, just an agent confidently reporting a partial list as
complete. The bounded-list contract exists because an unbounded payload tripped the
Vertex `GenerateContent` quota and turned the router's answers into empty-at-200
streams (docs/notes/router-empty-responses-quota.md). It was enforced by review
until these types; these tests are what make it enforced by CI.

**2. The two servers cannot share a types module, and that is a deploy fact.** Each
deploys with `gcloud run deploy --source src/mcp_servers/<name>` over a `COPY . .`
Dockerfile, so only that one directory ships. A shared
`src/mcp_servers/types.py` imports fine in the dev venv and `ImportError`s in the
container — the exact trap the servers' existing relative/absolute try-except
exists for. The first attempt at this conversion made that mistake.

So `booking` and `expense` each declare their own. These tests stand in for the
shared base class neither container could import: they assert the two independent
copies still agree, and — the part a base class could never do — that the shape
survives the *flat* import path the container actually uses.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path
from typing import get_type_hints

import pytest

_REPO = Path(__file__).resolve().parents[1]

#: (module, list-type name, records key) for every bounded list-returning tool.
BOUNDED_LISTS = [
    ("src.mcp_servers.booking.mock_db", "BookingList", "bookings"),
    ("src.mcp_servers.expense.mock_db", "ExpenseList", "expenses"),
]

#: The three keys every list tool owes its caller, whatever it lists.
CONTRACT = {"total_count", "returned_count", "truncated"}


class TestTheBoundedListContractHolds:
    @pytest.mark.parametrize(("mod", "name", "_records"), BOUNDED_LISTS)
    def test_the_three_contract_keys_are_declared(self, mod, name, _records):
        hints = get_type_hints(getattr(importlib.import_module(mod), name))
        missing = CONTRACT - set(hints)
        assert not missing, f"{name} drops {missing} — the model cannot report what it is not told"

    @pytest.mark.parametrize(("mod", "name", "_records"), BOUNDED_LISTS)
    def test_truncated_is_a_bool_not_a_count(self, mod, name, _records):
        """A caller asking `if result["truncated"]` must not be reading an int that
        happens to be 0."""
        hints = get_type_hints(getattr(importlib.import_module(mod), name))
        assert hints["truncated"] is bool

    @pytest.mark.parametrize(("mod", "name", "records"), BOUNDED_LISTS)
    def test_the_records_key_is_a_list_of_records(self, mod, name, records):
        hints = get_type_hints(getattr(importlib.import_module(mod), name))
        assert records in hints, f"{name} must name its records `{records}` — the model reads it"
        assert "list[" in str(hints[records])

    def test_the_two_servers_have_not_drifted_apart(self):
        """The point of the duplication test. Two deployables, one wire format — and
        no shared base class to keep them honest, because neither container could
        import one."""
        shapes = [
            set(get_type_hints(getattr(importlib.import_module(m), n))) & CONTRACT
            for m, n, _ in BOUNDED_LISTS
        ]
        assert shapes[0] == shapes[1] == CONTRACT


class TestTheTypesSurviveTheContainerImportPath:
    """The constraint that broke the first attempt, tested the only way that counts.

    `ty` and the test suite both run with the repo root importable, so a
    `from src.mcp_servers.types import ...` passes everything here and fails only
    once deployed. These tests run the import the way the container does: cwd inside
    the server directory, repo root NOT on the path.
    """

    @pytest.mark.parametrize("server", ["booking", "expense"])
    def test_mock_db_imports_flat_with_no_parent_package(self, server):
        d = _REPO / "src" / "mcp_servers" / server
        code = "import mock_db; print('ok')"
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=d,
            capture_output=True,
            text=True,
            env={"PYTHONPATH": "", "PATH": "/usr/bin:/bin"},
        )
        assert proc.returncode == 0, (
            f"{server}/mock_db.py cannot be imported the way its container does:\n{proc.stderr}"
        )

    @pytest.mark.parametrize("server", ["booking", "expense"])
    def test_no_parent_package_import_is_reintroduced(self, server):
        """Cheap belt to the subprocess braces: nothing under a server directory may
        reach up to `src.`, since `COPY . .` leaves that path nonexistent."""
        for py in (_REPO / "src" / "mcp_servers" / server).glob("*.py"):
            text = py.read_text()
            assert "from src." not in text and "import src." not in text, (
                f"{py.relative_to(_REPO)} imports from the parent package; "
                "only this directory ships to Cloud Run"
            )


class TestThePolicyVerdictKeepsItsTwoShapes:
    def test_an_unknown_category_omits_the_limit(self):
        """`check_policy` returns only `within_policy`/`reason` for an unknown
        category. Declaring `limit` required would make the real function's own
        return a type error; declaring it `NotRequired` is what keeps callers from
        reading it unguarded."""
        from src.mcp_servers.expense.mock_db import check_policy

        got = check_policy(10.0, "not-a-category")
        assert got["within_policy"] is False
        assert "limit" not in got

    def test_a_known_category_carries_the_limit(self):
        from src.mcp_servers.expense.mock_db import check_policy

        got = check_policy(10.0, "meals")
        assert got["within_policy"] is True
        assert got["limit"] == 75.00
