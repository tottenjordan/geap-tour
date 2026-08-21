"""The deployed-engine config baseline and the verifier that enforces it.

Everything here is pure: :func:`evaluate` takes a normalized spec dict, and the
CLI's fetch is injected, so no test touches GCP.

The fixtures are built from a ``_good_spec()`` helper that is deliberately
minimal-but-passing, so each test breaks exactly one thing. A test that asserted
against a hand-written 39-key env dict would pass for the wrong reason the first
time someone adds a check.
"""

import src.deploy.engine_baseline as eb
import src.deploy.verify_engine_config as v


def _good_env(engine_id="111"):
    return {
        "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
        "GOOGLE_GENAI_USE_VERTEXAI": "1",
        "GOOGLE_GENAI_USE_ENTERPRISE": "1",
        "SEARCH_MCP_SERVER": "projects/p/locations/l/mcpServers/s",
        "BOOKING_MCP_SERVER": "projects/p/locations/l/mcpServers/b",
        "EXPENSE_MCP_SERVER": "projects/p/locations/l/mcpServers/e",
        "AGENT_ENGINE_ID": engine_id,
        # coordinator
        "ENABLE_MEMORY_BANK": "1",
        "ENABLE_MEMORY_PRELOAD_CACHE": "1",
        "COORDINATOR_MODEL": "gemini-2.5-flash",
        # router
        "LITE_MODEL": "gemini-2.5-flash-lite",
        "FLASH_MODEL": "gemini-2.5-flash",
        "PRO_MODEL": "gemini-2.5-pro",
        "CLASSIFIER_MODEL": "gemini-2.5-flash-lite",
    }


def _good_spec(engine_id="111", display_name="coordinator_agent_jt1", **over):
    spec = {
        "engine_id": engine_id,
        "display_name": display_name,
        "identity_type": "AGENT_IDENTITY",
        "min_instances": 4,
        "resource_limits": {"cpu": eb.LITELLM_CPU, "memory": eb.LITELLM_MEMORY},
        "env": _good_env(engine_id),
    }
    spec.update(over)
    return spec


def _find(findings, name):
    return next(f for f in findings if f.name == name)


class TestBaselinePasses:
    def test_a_fully_configured_coordinator_has_no_findings(self):
        findings = eb.evaluate(_good_spec(), "coordinator")
        assert all(f.ok for f in findings), [f.name for f in findings if not f.ok]

    def test_a_fully_configured_router_has_no_findings(self):
        spec = _good_spec(display_name="router_agent_jt1")
        findings = eb.evaluate(spec, "router")
        assert all(f.ok for f in findings), [f.name for f in findings if not f.ok]

    def test_role_is_inferred_from_the_display_name(self):
        assert eb.infer_role(_good_spec(display_name="router_agent_jt1")) == "router"
        assert eb.infer_role(_good_spec(display_name="coordinator_agent")) == "coordinator"

    def test_an_unrecognised_name_gets_the_shared_checks_only(self):
        """Leaf agents (travel/expense/tiers) are neither role — they still need
        memory, identity and telemetry, but have no tier or memory-bank wiring."""
        spec = _good_spec(display_name="travel_agent")
        assert eb.infer_role(spec) == "unknown"
        names = {f.name for f in eb.evaluate(spec)}
        assert "memory" in names
        assert "tier_models_pinned" not in names
        assert "memory_bank" not in names


class TestCriticalDrift:
    def test_the_4gi_platform_default_is_critical(self):
        """The whole reason this module exists: an engine with no resource_limits
        is on 4Gi, which OOM-kills workers and returns empty-at-200."""
        spec = _good_spec(resource_limits=None)
        f = _find(eb.evaluate(spec, "coordinator"), "memory")
        assert not f.ok
        assert f.severity == "critical"
        assert "4Gi" in f.observed

    def test_a_smaller_explicit_limit_is_still_critical(self):
        spec = _good_spec(resource_limits={"cpu": "4", "memory": "8Gi"})
        assert not _find(eb.evaluate(spec, "coordinator"), "memory").ok

    def test_default_identity_is_critical(self):
        spec = _good_spec(identity_type=None)
        f = _find(eb.evaluate(spec, "coordinator"), "identity")
        assert not f.ok and f.severity == "critical"

    def test_telemetry_off_is_critical(self):
        env = _good_env()
        env["GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"] = "false"
        assert not _find(eb.evaluate(_good_spec(env=env), "coordinator"), "telemetry").ok

    def test_a_missing_mcp_registry_name_is_critical_and_names_it(self):
        env = _good_env()
        env["BOOKING_MCP_SERVER"] = ""
        f = _find(eb.evaluate(_good_spec(env=env), "coordinator"), "mcp_registry_names")
        assert not f.ok
        assert "BOOKING_MCP_SERVER" in f.observed

    def test_memory_bank_off_is_critical_for_the_coordinator(self):
        env = _good_env()
        env["ENABLE_MEMORY_BANK"] = "0"
        assert not _find(eb.evaluate(_good_spec(env=env), "coordinator"), "memory_bank").ok

    def test_has_critical_drift_ignores_advisories(self):
        env = _good_env()
        env["ENABLE_MEMORY_PRELOAD_CACHE"] = ""  # advisory only
        findings = eb.evaluate(_good_spec(env=env), "coordinator")
        assert not _find(findings, "memory_preload_cache").ok
        assert not eb.has_critical_drift(findings)


class TestRouterTrap:
    """The router's two silent-regression traps, which nothing else catches."""

    def test_gemini3_tier_models_are_critical(self):
        """A plain `deploy_agents router --update` bakes the Gemini-3 defaults.
        The deploy succeeds and the engine serves — on the wrong models."""
        env = _good_env()
        env["FLASH_MODEL"] = "gemini-3.5-flash"
        spec = _good_spec(display_name="router_agent", env=env)
        f = _find(eb.evaluate(spec, "router"), "tier_models_pinned")
        assert not f.ok
        assert "gemini-3.5-flash" in f.observed

    def test_all_three_tiers_are_reported_not_just_the_broken_one(self):
        f = _find(
            eb.evaluate(_good_spec(display_name="router_agent"), "router"), "tier_models_pinned"
        )
        assert "lite=" in f.observed and "flash=" in f.observed and "pro=" in f.observed

    def test_a_thinking_classifier_is_critical(self):
        """A thinking model returns empty text, so every prompt takes the
        low-score fallback and all traffic collapses onto the lite tier."""
        env = _good_env()
        env["CLASSIFIER_MODEL"] = "gemini-3.5-flash"
        spec = _good_spec(display_name="router_agent", env=env)
        assert not _find(eb.evaluate(spec, "router"), "classifier_non_thinking").ok


class TestAdvisories:
    def test_scale_to_zero_is_advisory_not_critical(self):
        """The one measurement blaming min_instances predates the OOM fix and is
        confounded by it, so this is a latency floor, not a proven empty fix."""
        spec = _good_spec(min_instances=None)
        f = _find(eb.evaluate(spec, "coordinator"), "min_instances")
        assert not f.ok
        assert f.severity == "advisory"
        assert "scale to zero" in f.observed

    def test_gemini3_coordinator_reports_armor_as_client_side_only(self):
        env = _good_env()
        env["COORDINATOR_MODEL"] = "gemini-3.5-flash"
        f = _find(eb.evaluate(_good_spec(env=env), "coordinator"), "server_side_armor")
        assert not f.ok
        assert f.severity == "advisory"
        assert "client-side guardrail only" in f.observed

    def test_a_claude_coordinator_also_loses_server_side_armor(self):
        env = _good_env()
        env["COORDINATOR_MODEL"] = "claude-sonnet-5"
        assert not _find(eb.evaluate(_good_spec(env=env), "coordinator"), "server_side_armor").ok

    def test_a_foreign_baked_engine_id_is_advisory(self):
        """Sessions/memory are safe (the runtime injects its own id), but
        config-derived client values and logs still read wrong."""
        env = _good_env(engine_id="999")
        f = _find(eb.evaluate(_good_spec(engine_id="111", env=env), "coordinator"), "own_engine_id")
        assert not f.ok and f.severity == "advisory"


class TestNormalize:
    def test_flattens_the_api_resource(self):
        spec = v.normalize(
            {
                "name": "projects/p/locations/us-central1/reasoningEngines/777",
                "displayName": "coordinator_agent",
                "updateTime": "2026-08-21T17:12:56.123456Z",
                "labels": {"solution": "geap-tour"},
                "spec": {
                    "identityType": "AGENT_IDENTITY",
                    "deploymentSpec": {
                        "minInstances": 4,
                        "resourceLimits": {"cpu": "4", "memory": "16Gi"},
                        "env": [{"name": "A", "value": "1"}, {"name": "B", "value": "2"}],
                    },
                },
            }
        )
        assert spec["engine_id"] == "777"
        assert spec["min_instances"] == 4
        assert spec["resource_limits"] == {"cpu": "4", "memory": "16Gi"}
        assert spec["env"] == {"A": "1", "B": "2"}
        assert spec["labels"] == {"solution": "geap-tour"}

    def test_absent_deployment_spec_does_not_crash(self):
        """A create still in progress has no deploymentSpec; that must read as
        'unset' (and fail the memory check), not raise."""
        spec = v.normalize({"name": "p/l/reasoningEngines/1", "spec": {}})
        assert spec["resource_limits"] is None
        assert spec["env"] == {}
        assert not _find(eb.evaluate(spec, "coordinator"), "memory").ok


class TestCli:
    def _fetch(self, resource):
        return lambda engine_id: resource

    def _resource(self, engine_id="111", **deployment):
        ds = {
            "minInstances": 4,
            "resourceLimits": {"cpu": "4", "memory": "16Gi"},
            "env": [{"name": k, "value": val} for k, val in _good_env(engine_id).items()],
        }
        ds.update(deployment)
        return {
            "name": f"projects/p/locations/l/reasoningEngines/{engine_id}",
            "displayName": "coordinator_agent",
            "spec": {"identityType": "AGENT_IDENTITY", "deploymentSpec": ds},
        }

    def test_exit_zero_when_clean(self, capsys):
        code = v.main(["--engine-id", "111"], fetch=self._fetch(self._resource()))
        assert code == 0
        assert "config: PASS" in capsys.readouterr().out

    def test_exit_nonzero_on_critical_drift(self, capsys):
        resource = self._resource(resourceLimits=None)
        code = v.main(["--engine-id", "111"], fetch=self._fetch(resource))
        assert code == 1
        assert "config: FAIL" in capsys.readouterr().out

    def test_advisory_alone_still_exits_zero(self):
        """An advisory must never fail CI, or nobody will run this in CI."""
        resource = self._resource(minInstances=None)
        assert v.main(["--engine-id", "111"], fetch=self._fetch(resource)) == 0

    def test_an_unreachable_engine_is_a_failure_not_a_traceback(self, capsys):
        def boom(engine_id):
            raise RuntimeError("403 permission denied")

        assert v.main(["--engine-id", "111"], fetch=boom) == 1
        assert "UNREACHABLE" in capsys.readouterr().out

    def test_json_output_is_serializable(self, capsys):
        import json

        v.main(["--engine-id", "111", "--json"], fetch=self._fetch(self._resource()))
        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["engine_id"] == "111"
        assert all("severity" in f for f in payload[0]["findings"])

    def test_why_prints_the_rationale(self, capsys):
        v.main(["--engine-id", "111", "--why"], fetch=self._fetch(self._resource()))
        assert "why:" in capsys.readouterr().out

    def test_role_override_beats_inference(self, capsys):
        """A router deployed under a coordinator-ish display name still needs the
        router rules; --role is the escape hatch."""
        v.main(["--engine-id", "111", "--role", "router"], fetch=self._fetch(self._resource()))
        assert "tier_models_pinned" in capsys.readouterr().out


class TestEngineExists:
    """A deleted engine id must read as 'not usable', not blow up a preflight."""

    def test_a_reachable_engine_exists(self):
        assert v.engine_exists("111", fetch=lambda _e: {"name": "x"}) is True

    def test_a_404_does_not_exist(self):
        def gone(_engine_id):
            raise RuntimeError("404 not found")

        assert v.engine_exists("111", fetch=gone) is False

    def test_an_empty_id_short_circuits(self):
        def explode(_engine_id):
            raise AssertionError("must not call the API for an unset id")

        assert v.engine_exists("", fetch=explode) is False


class TestBaselineIsSharedWithTheDeployer:
    def test_expectations_come_from_deploy_agents_not_a_copy(self):
        """If these drifted apart, the verifier would bless a config the deployer
        never produces (or vice versa) — the exact failure this module prevents."""
        import src.deploy.deploy_agents as da

        assert eb.LITELLM_MEMORY is da.LITELLM_MEMORY
        assert eb.LITELLM_CPU is da.LITELLM_CPU

    def test_every_check_explains_itself(self):
        """A check with no rationale gets deleted by the next person who hits it."""
        for role in (*eb.ROLES, "unknown"):
            for check in eb.checks_for(role):
                assert len(check.why) > 40, check.name
                assert check.severity in ("critical", "advisory")
