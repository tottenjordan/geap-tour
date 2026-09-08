"""Deployment outputs must reach `.env` intact, and must not reformat it.

`.env` is the source of truth for every deployed identifier here — `src/config.py`
reads it, `verify_engine_config` checks against it, `find_orphan_engines`
reconciles with it. Anything a deploy produces and does not write back has to be
copy-pasted by hand, and a value nobody copies goes stale silently. That is how a
deleted engine stayed referenced for 3.5 months and how `setup_apphub.sh` came to
default to two engines that no longer existed.

The specific bug this file guards: the previous writer lived inside `deploy_agents`
and unconditionally applied `value.split("/")[-1]`. Correct for shortening a
resource name to an engine id, and silently destructive for a URL —
`https://search-mcp-abc.run.app` became `search-mcp-abc.run.app`, scheme gone.

Pure: every test writes to a tmp_path file.
"""

from __future__ import annotations

from src.deploy.env_file import set_env_var


class TestValuesAreStoredVerbatim:
    def test_a_url_keeps_its_scheme(self, tmp_path):
        """THE regression. The old writer's split("/")[-1] silently dropped
        `https://`, and a mangled MCP URL fails at request time, not at deploy."""
        p = tmp_path / ".env"
        url = "https://search-mcp-abc.us-central1.run.app/mcp"
        set_env_var("SEARCH_MCP_URL", url, path=str(p), quiet=True)
        assert p.read_text() == f"SEARCH_MCP_URL={url}\n"

    def test_an_engine_id_is_not_reinterpreted(self, tmp_path):
        p = tmp_path / ".env"
        set_env_var("ROUTER_ENGINE_ID", "6134089059699523584", path=str(p), quiet=True)
        assert p.read_text() == "ROUTER_ENGINE_ID=6134089059699523584\n"


class TestItEditsInPlaceRatherThanRewriting:
    def test_surrounding_lines_and_comments_survive(self, tmp_path):
        """A deploy must never reformat a file the user maintains by hand."""
        p = tmp_path / ".env"
        p.write_text("# header\nA=1\nROUTER_ENGINE_ID=old\n# trailing note\nB=2\n")
        set_env_var("ROUTER_ENGINE_ID", "new", path=str(p), quiet=True)
        assert p.read_text() == "# header\nA=1\nROUTER_ENGINE_ID=new\n# trailing note\nB=2\n"

    def test_a_missing_variable_is_appended(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("A=1\n")
        set_env_var("B", "2", path=str(p), quiet=True)
        assert p.read_text() == "A=1\nB=2\n"

    def test_a_file_without_a_trailing_newline_is_not_spliced(self, tmp_path):
        """Otherwise the new entry lands on the end of the previous line and both
        variables are quietly destroyed."""
        p = tmp_path / ".env"
        p.write_text("A=1")
        set_env_var("B", "2", path=str(p), quiet=True)
        assert p.read_text() == "A=1\nB=2\n"

    def test_a_missing_file_is_created(self, tmp_path):
        p = tmp_path / ".env"
        set_env_var("A", "1", path=str(p), quiet=True)
        assert p.read_text() == "A=1\n"

    def test_a_prefix_match_is_not_treated_as_the_variable(self, tmp_path):
        """`startswith(f"{name}=")` would be fine, but a naive `in` check or a
        shortened name must not overwrite FLASH_ENGINE_ID when asked for ENGINE_ID."""
        p = tmp_path / ".env"
        p.write_text("FLASH_ENGINE_ID=aaa\nENGINE_ID=bbb\n")
        set_env_var("ENGINE_ID", "ccc", path=str(p), quiet=True)
        assert p.read_text() == "FLASH_ENGINE_ID=aaa\nENGINE_ID=ccc\n"

    def test_a_commented_out_line_is_not_the_variable(self, tmp_path):
        """Uncommenting someone's deliberately-disabled line would be a surprise."""
        p = tmp_path / ".env"
        p.write_text("# ROUTER_ENGINE_ID=disabled\n")
        set_env_var("ROUTER_ENGINE_ID", "live", path=str(p), quiet=True)
        assert p.read_text() == "# ROUTER_ENGINE_ID=disabled\nROUTER_ENGINE_ID=live\n"


class TestItReportsWhetherItChangedAnything:
    def test_an_unchanged_value_is_a_no_op(self, tmp_path):
        """Re-running a deploy should not churn the file or print a misleading
        'updated' line for a value that already matched."""
        p = tmp_path / ".env"
        p.write_text("A=1\n")
        assert set_env_var("A", "1", path=str(p), quiet=True) is False

    def test_a_changed_value_reports_true(self, tmp_path):
        p = tmp_path / ".env"
        p.write_text("A=1\n")
        assert set_env_var("A", "2", path=str(p), quiet=True) is True


class TestBothDeployPathsWriteBack:
    """The user-visible point: a deploy records what it produced."""

    def test_agent_deploys_record_the_engine_id_shortened(self, tmp_path, monkeypatch):
        """deploy_agents shortens the resource name AT THE CALL SITE, because the
        writer itself must stay verbatim for the URL case."""
        import src.deploy.deploy_agents as da

        p = tmp_path / ".env"
        monkeypatch.setattr(da, "ENV_FILE", str(p))
        da._update_env_file("ROUTER_ENGINE_ID", "projects/p/locations/l/reasoningEngines/12345")
        assert p.read_text() == "ROUTER_ENGINE_ID=12345\n"

    def test_mcp_deploys_record_their_urls(self, tmp_path, monkeypatch):
        """These URLs previously only reached stdout, and config defaults them to
        http://localhost:800x/mcp — so a missed copy-paste does not fail loudly, it
        points the registry fallback at a local port that is not listening."""
        import src.deploy.deploy_mcp_servers as dm
        import src.deploy.env_file as ef

        written: dict[str, str] = {}
        monkeypatch.setattr(ef, "ENV_FILE", str(tmp_path / ".env"))
        monkeypatch.setattr(dm, "deploy_server", lambda s: f"https://{s['name']}-abc.run.app")
        monkeypatch.setattr(ef, "set_env_var", lambda n, v, **k: written.__setitem__(n, v) or True)
        dm.deploy_all_servers()
        assert written == {
            "SEARCH_MCP_URL": "https://search-mcp-abc.run.app/mcp",
            "BOOKING_MCP_URL": "https://booking-mcp-abc.run.app/mcp",
            "EXPENSE_MCP_URL": "https://expense-mcp-abc.run.app/mcp",
        }

    def test_an_empty_url_is_not_recorded(self, tmp_path, monkeypatch):
        """A failed describe returns "". Writing that would replace a working URL
        with nothing — worse than leaving the previous value in place."""
        import src.deploy.deploy_mcp_servers as dm
        import src.deploy.env_file as ef

        written: dict[str, str] = {}
        monkeypatch.setattr(dm, "deploy_server", lambda s: "")
        monkeypatch.setattr(ef, "set_env_var", lambda n, v, **k: written.__setitem__(n, v) or True)
        dm.deploy_all_servers()
        assert written == {}

    def test_every_mcp_server_declares_where_its_url_goes(self):
        """A new server without an env_var would deploy and silently not be
        recorded — the exact gap this closes."""
        from src.deploy.deploy_mcp_servers import SERVERS

        assert SERVERS
        for s in SERVERS:
            assert s.get("env_var", "").endswith("_MCP_URL"), s.get("name")
