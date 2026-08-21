"""The cross-model experiment refuses to run against engines that no longer exist.

`.env` keeps tier engine ids long after the engines are deleted — at the time this
was written, all five named dead engines. Building a resource name from an id is
pure string formatting, so it always succeeds and the failure surfaces deep inside
an eval, after the run has already cost time and money.

`exists_fn` is injected, so nothing here touches GCP.
"""

import pytest

from src.eval import cross_model_experiment as cme


def _agents():
    return list(cme.EXPERIMENT_AGENTS.keys())


class TestPreflightEngines:
    def test_all_live_reports_nothing(self, monkeypatch):
        monkeypatch.setattr(cme, "EXPERIMENT_AGENTS", {"lite_agent": "111"})
        assert cme.preflight_engines(["lite_agent"], exists_fn=lambda _id: True) == []

    def test_a_dead_engine_is_reported_with_its_env_var(self, monkeypatch):
        """Naming the variable is the point — "lite_agent is broken" doesn't tell
        you which of five `.env` lines to fix."""
        monkeypatch.setattr(cme, "EXPERIMENT_AGENTS", {"lite_agent": "dead-id"})
        [line] = cme.preflight_engines(["lite_agent"], exists_fn=lambda _id: False)
        assert "LITE_ENGINE_ID" in line
        assert "dead-id" in line

    def test_an_unset_id_is_not_a_preflight_failure(self, monkeypatch):
        """Unset is already handled downstream as a deliberate skip; only a *set*
        id pointing at nothing is the silent-failure case."""
        monkeypatch.setattr(cme, "EXPERIMENT_AGENTS", {"lite_agent": ""})

        def explode(_id):
            raise AssertionError("must not check an unset id")

        assert cme.preflight_engines(["lite_agent"], exists_fn=explode) == []

    def test_every_agent_has_a_named_env_var(self):
        """A new tier agent without a mapping would report '?' — catch it here."""
        assert set(cme.ENGINE_ENV_VARS) == set(_agents())

    def test_reports_all_dead_engines_not_just_the_first(self, monkeypatch):
        monkeypatch.setattr(cme, "EXPERIMENT_AGENTS", {"lite_agent": "a", "pro_agent": "b"})
        lines = cme.preflight_engines(["lite_agent", "pro_agent"], exists_fn=lambda _id: False)
        assert len(lines) == 2


class TestRunExperimentGuard:
    def test_raises_before_spending_anything(self, monkeypatch):
        """The guard must fire before vertexai.init / Client / any eval call."""
        monkeypatch.setattr(cme, "EXPERIMENT_AGENTS", {"lite_agent": "dead-id"})

        def explode(*a, **k):
            raise AssertionError("must not initialize Vertex when preflight fails")

        monkeypatch.setattr(cme.vertexai, "init", explode)

        with pytest.raises(RuntimeError, match="do not exist"):
            cme.run_experiment(agents=["lite_agent"], exists_fn=lambda _id: False)

    def test_the_error_says_how_to_fix_it(self, monkeypatch):
        monkeypatch.setattr(cme, "EXPERIMENT_AGENTS", {"lite_agent": "dead-id"})
        with pytest.raises(RuntimeError) as exc:
            cme.run_experiment(agents=["lite_agent"], exists_fn=lambda _id: False)
        message = str(exc.value)
        assert "Redeploy the tier agents" in message
        assert "LITE_ENGINE_ID" in message
