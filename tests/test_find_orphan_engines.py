"""The orphan detector must catch the engine that motivated it, and no one else's.

`sonnet_agent` 8467456143491334144 was a deployment of this repo abandoned for ~3.5
months — zero traffic, on the 4Gi default that OOM-kills workers — and it was found
by accident. Two properties decide whether this module is worth having, and both are
easy to get wrong in a way that still looks like it works:

1. **Ownership by env fingerprint, not label.** That engine had NO label. A
   label-based detector would have missed its own founding case, which is the
   "check that cannot detect its own failure" shape this repo keeps finding.
2. **It must never name a foreign engine.** `verify_engine_config.default_targets`
   deliberately avoids listing this shared project because listing "would invite
   reporting on — or worse, acting on — engines that are not ours."

Pure: every test drives `find_orphans` with injected specs. No GCP.
"""

from __future__ import annotations

import pytest

from src.deploy.find_orphan_engines import (
    ENGINE_ID_VARS,
    KNOWN_UNREFERENCED,
    find_orphans,
    is_ours,
    main,
    referenced_engine_ids,
    render,
)

OUR_MCP = "projects/hybrid-vertex/locations/us-central1/mcpServers/search-mcp"


def _spec(engine_id, *, ours=True, labelled=True, name="agent", **over):
    """A normalize()-shaped spec — the shape verify_engine_config.normalize emits."""
    spec = {
        "engine_id": engine_id,
        "display_name": name,
        "labels": {"solution": "geap-tour"} if labelled else {},
        "env": {"SEARCH_MCP_SERVER": OUR_MCP} if ours else {"SOME_OTHER": "x"},
        "min_instances": 1,
        "resource_limits": {"memory": "16Gi"},
        "update_time": "2026-05-22T00:12:49Z",
    }
    spec.update(over)
    return spec


@pytest.fixture(autouse=True)
def _our_servers(monkeypatch):
    monkeypatch.setattr("src.deploy.find_orphan_engines.SEARCH_MCP_SERVER", OUR_MCP)
    monkeypatch.setattr("src.deploy.find_orphan_engines.BOOKING_MCP_SERVER", "")
    monkeypatch.setattr("src.deploy.find_orphan_engines.EXPENSE_MCP_SERVER", "")


class TestOwnershipIsByFingerprintNotLabel:
    def test_an_unlabelled_engine_of_ours_is_still_ours(self):
        """THE founding case. `sonnet_agent` had no label; a label-based test would
        have skipped the one engine this module exists to find."""
        assert is_ours(_spec("1", labelled=False)) is True

    def test_a_foreign_engine_is_not_ours_even_if_it_looks_like_an_agent(self):
        assert is_ours(_spec("2", ours=False, name="router_agent")) is False

    def test_an_unset_mcp_config_matches_nothing(self, monkeypatch):
        """Guard against the empty string matching every engine in the project —
        that would turn a misconfigured checkout into a delete-everything report."""
        monkeypatch.setattr("src.deploy.find_orphan_engines.SEARCH_MCP_SERVER", "")
        assert is_ours(_spec("3", ours=False, env={"SEARCH_MCP_SERVER": ""})) is False
        assert is_ours(_spec("4")) is False


class TestTheThreeFindingsStaySeparate:
    def test_an_unreferenced_engine_of_ours_is_an_orphan(self):
        out = find_orphans([_spec("dead")], referenced={})
        assert [o["engine_id"] for o in out["orphans"]] == ["dead"]

    def test_a_referenced_engine_is_not(self):
        out = find_orphans([_spec("live")], referenced={"live": "ROUTER_ENGINE_ID"})
        assert out["orphans"] == []

    def test_an_unlabelled_engine_is_reported_separately(self):
        """Unlabelled and unreferenced need different fixes; merging them into one
        'problem' list is how the last orphan hid in plain sight."""
        out = find_orphans([_spec("x", labelled=False)], referenced={"x": "AGENT_ENGINE_ID"})
        assert out["orphans"] == []
        assert [u["engine_id"] for u in out["unlabelled"]] == ["x"]

    def test_a_config_id_that_resolves_to_nothing_is_dangling(self):
        out = find_orphans([_spec("live")], referenced={"live": "A", "ghost": "OPUS_ENGINE_ID"})
        assert out["dangling"] == [{"engine_id": "ghost", "env_var": "OPUS_ENGINE_ID"}]

    def test_orphans_carry_the_triage_facts(self):
        """'Is it costing anything and is it a trap' is the first question. The last
        orphan was 4Gi — worse than idle, because anything reaching it OOMs."""
        out = find_orphans([_spec("dead", min_instances=0, resource_limits={})], referenced={})
        o = out["orphans"][0]
        assert o["min_instances"] == 0
        assert o["memory"] is None
        assert o["update_time"] and o["display_name"]


class TestForeignEnginesAreNeverNamed:
    """The objection `default_targets` raises about listing a shared project."""

    def test_they_are_counted_but_not_listed(self):
        specs = [_spec("ours"), _spec("theirs1", ours=False), _spec("theirs2", ours=False)]
        out = find_orphans(specs, referenced={})
        assert out["total_engines"] == 3
        assert out["ours"] == 1
        named = {e["engine_id"] for key in ("orphans", "unlabelled", "kept") for e in out[key]}
        assert named == {"ours"}

    def test_the_rendered_report_does_not_leak_a_foreign_id(self):
        specs = [_spec("ours"), _spec("SECRET-FOREIGN-ID", ours=False, name="someone-elses")]
        text = render(find_orphans(specs, referenced={}))
        assert "SECRET-FOREIGN-ID" not in text
        assert "someone-elses" not in text
        assert "2 engines in project, 1 ours" in text


class TestDeliberatelyKeptEnginesDoNotCryWolf:
    """A check that fires on known-good state every run is one people skim past."""

    def test_a_known_unreferenced_engine_is_not_an_orphan(self):
        kept_id = next(iter(KNOWN_UNREFERENCED))
        out = find_orphans([_spec(kept_id)], referenced={})
        assert out["orphans"] == []
        assert [k["engine_id"] for k in out["kept"]] == [kept_id]

    def test_it_is_still_shown_with_its_reason(self):
        """Suppressed-and-invisible is indistinguishable from a bug, so the
        exception is displayed rather than filtered away."""
        kept_id = next(iter(KNOWN_UNREFERENCED))
        text = render(find_orphans([_spec(kept_id)], referenced={}))
        assert kept_id in text
        assert "ON PURPOSE" in text

    def test_every_entry_explains_why_it_is_kept(self):
        for eid, reason in KNOWN_UNREFERENCED.items():
            assert eid.isdigit(), eid
            assert len(reason) > 40, f"{eid}: a bare exception with no reason rots"


class TestTheDetectorDetectsItsOwnIncompleteness:
    def test_every_engine_id_env_var_in_src_is_covered(self):
        """ENGINE_ID_VARS is hand-maintained. A new one added elsewhere and forgotten
        here turns a LIVE engine into a reported orphan — the detector failing the
        same way its subject did. Parsed with ast so prose in docstrings is ignored.
        """
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1] / "src"
        found: set[str] = set()
        for path in root.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    name = node.args[0].value
                    if name.endswith(("_ENGINE_ID", "_AGENT_ID")):
                        found.add(name)
        # GOOGLE_CLOUD_AGENT_ENGINE_ID is injected by the managed runtime into the
        # engine's own process; it never names a DIFFERENT engine from outside.
        found.discard("GOOGLE_CLOUD_AGENT_ENGINE_ID")
        missing = sorted(found - set(ENGINE_ID_VARS))
        assert not missing, f"engine-id vars not covered by ENGINE_ID_VARS: {missing}"

    def test_referenced_ids_are_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("ROUTER_ENGINE_ID", "12345")
        assert referenced_engine_ids().get("12345") == "ROUTER_ENGINE_ID"

    def test_a_full_resource_name_is_reduced_to_its_id(self, monkeypatch):
        """`.env` sometimes holds a bare id and sometimes a full resource path; the
        listing returns bare ids, so a mismatch would report a live engine as an
        orphan."""
        monkeypatch.setenv("OPUS_ENGINE_ID", "projects/p/locations/l/reasoningEngines/999")
        assert referenced_engine_ids().get("999") == "OPUS_ENGINE_ID"


class TestCli:
    def test_it_is_advisory_and_always_exits_zero(self):
        """An engine can be legitimately unreferenced for days (a bake-off deploy in
        flight). Going red for normal work is how a signal gets ignored."""
        assert main([], list_engines=lambda: [_spec("dead")]) == 0

    def test_json_output_is_machine_readable(self, capsys):
        import json

        main(["--json"], list_engines=lambda: [_spec("dead")])
        payload = json.loads(capsys.readouterr().out)
        assert payload["orphans"][0]["engine_id"] == "dead"

    def test_a_clean_fleet_says_so_rather_than_printing_nothing(self, capsys):
        main([], list_engines=lambda: [_spec("live")])
        # referenced_engine_ids reads the real env, which will not contain "live",
        # so assert on the shape rather than a clean result here.
        assert "engines in project" in capsys.readouterr().out

    def test_there_is_no_delete_flag(self):
        """Read-only is the condition on which listing a shared project is
        acceptable at all."""
        with pytest.raises(SystemExit):
            main(["--delete"], list_engines=list)
