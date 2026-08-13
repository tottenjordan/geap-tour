"""Offline tests for the dormant Vertex Experiments logging helper (no live GCP).

``log_run`` is a thin wrapper over ``google.cloud.aiplatform``. A fake aiplatform
module is injected so the call sequence (init → start_run → log_params →
log_metrics → end_run) and the no-op-when-unset behavior are verified without
network or credentials.
"""

from src.observability.experiments import log_run


class _FakeRun:
    def __init__(self, fake):
        self._fake = fake

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fake.calls.append(("end_run",))
        return False


class _FakeAiPlatform:
    """Records the ordered sequence of aiplatform calls."""

    def __init__(self):
        self.calls = []

    def init(self, **kwargs):
        self.calls.append(("init", kwargs))

    def start_run(self, run, **kwargs):
        self.calls.append(("start_run", run, kwargs))
        return _FakeRun(self)

    def log_params(self, params):
        self.calls.append(("log_params", params))

    def log_metrics(self, metrics):
        self.calls.append(("log_metrics", metrics))


class TestLogRun:
    def test_logs_params_then_metrics_in_order(self):
        fake = _FakeAiPlatform()
        log_run(
            experiment="coordinator-bakeoff",
            run="gemini-3-6-flash",
            params={"backbone": "gemini-3.6-flash"},
            metrics={"pairwise_win_rate": 0.6, "cost_per_request": 0.0012},
            aiplatform=fake,
        )

        names = [c[0] for c in fake.calls]
        assert names.index("init") < names.index("start_run")
        assert names.index("start_run") < names.index("log_params")
        assert names.index("log_params") < names.index("log_metrics")

        init_kwargs = fake.calls[0][1]
        assert init_kwargs["experiment"] == "coordinator-bakeoff"
        assert fake.calls[names.index("start_run")][1] == "gemini-3-6-flash"
        assert fake.calls[names.index("log_params")][1] == {"backbone": "gemini-3.6-flash"}
        assert fake.calls[names.index("log_metrics")][1] == {
            "pairwise_win_rate": 0.6,
            "cost_per_request": 0.0012,
        }

    def test_returns_true_on_success(self):
        fake = _FakeAiPlatform()
        assert (
            log_run(
                experiment="coordinator-bakeoff",
                run="claude-sonnet-5",
                params={"backbone": "claude-sonnet-5"},
                metrics={"pairwise_win_rate": 0.4},
                aiplatform=fake,
            )
            is True
        )

    def test_noop_when_experiment_unset(self):
        fake = _FakeAiPlatform()
        result = log_run(
            experiment=None,
            run="gemini",
            params={"backbone": "gemini"},
            metrics={"x": 1.0},
            aiplatform=fake,
        )
        assert result is False
        assert fake.calls == []  # never touched aiplatform

    def test_noop_when_experiment_blank(self):
        fake = _FakeAiPlatform()
        assert (
            log_run(
                experiment="   ",
                run="gemini",
                params={},
                metrics={"x": 1.0},
                aiplatform=fake,
            )
            is False
        )
        assert fake.calls == []

    def test_coerces_metrics_to_float_and_drops_non_numeric(self):
        fake = _FakeAiPlatform()
        log_run(
            experiment="coordinator-bakeoff",
            run="gemini",
            params={"backbone": "gemini"},
            metrics={"win_rate": 0.6, "note": "n/a", "count": 3},
            aiplatform=fake,
        )
        logged = next(c[1] for c in fake.calls if c[0] == "log_metrics")
        assert logged == {"win_rate": 0.6, "count": 3.0}
        assert all(isinstance(v, float) for v in logged.values())

    def test_swallows_backend_errors(self):
        class _Boom(_FakeAiPlatform):
            def start_run(self, run, **kwargs):
                raise RuntimeError("no credentials")

        # Best-effort: a backend failure must not propagate to the caller.
        assert (
            log_run(
                experiment="coordinator-bakeoff",
                run="gemini",
                params={"backbone": "gemini"},
                metrics={"x": 1.0},
                aiplatform=_Boom(),
            )
            is False
        )
