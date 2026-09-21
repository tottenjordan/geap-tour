"""Every monitored surface must be able to reach its rolling baseline.

`agent_router_quality/*` could not. The arithmetic:

    lookback window     48h   (verify_monitors.DEFAULT_LOOKBACK_HOURS)
    points needed        5    (baseline.MIN_BASELINE)
    daily writer yields  2    points in that window

So the z-score detector reported `insufficient_history` on every run and always
would have. Not a cron failure — `router_quality.yaml` fired 5/5 days — and not a
missing series: the points exist, the reader's window just throws them away before
counting. The static floors still worked, so half the surface's protection was
live and the other half was structurally dead while looking configured.

CLAUDE.md said the opposite: "`baseline.MIN_BASELINE` (5) means the rolling z-score
goes live after **5 days**". True of the data, false of the reader.

The fix is a per-surface window, and the guard is the invariant rather than the
number: each surface declares how often it is written, and a window that cannot
accumulate `MIN_BASELINE + 1` points at that cadence fails here. A future weekly
series then cannot be added with an hourly surface's window.
"""

from __future__ import annotations

from datetime import UTC

import pytest

from src.eval.baseline import MIN_BASELINE
from src.eval.verify_monitors import DEFAULT_LOOKBACK_HOURS, SURFACES


@pytest.mark.parametrize("name", sorted(SURFACES))
def test_the_window_can_accumulate_a_baseline(name):
    """`MIN_BASELINE + 1`: the detector needs a history to compare the CURRENT
    point against, so a window holding exactly MIN_BASELINE is still one short."""
    s = SURFACES[name]
    expected = s.lookback_hours / 24.0 * s.writes_per_day
    assert expected >= MIN_BASELINE + 1, (
        f"{name}: a {s.writes_per_day}/day writer yields ~{expected:.1f} points in "
        f"{s.lookback_hours}h, below MIN_BASELINE+1 ({MIN_BASELINE + 1}). Its rolling "
        "baseline can never fire; widen lookback_hours."
    )


@pytest.mark.parametrize("name", sorted(SURFACES))
def test_the_declared_cadence_is_plausible(name):
    """A surface could satisfy the check above by overstating how often it writes."""
    s = SURFACES[name]
    assert 0 < s.writes_per_day <= 24, f"{name}: implausible cadence {s.writes_per_day}"


def test_the_daily_surface_is_the_one_that_needed_widening():
    """Pins the specific case, so the general invariant above cannot be satisfied
    by quietly relabelling router_quality as hourly."""
    rq = SURFACES["router_quality"]
    assert rq.writes_per_day == 1.0, "router_quality.yaml is a daily cron (37 4 * * *)"
    assert rq.lookback_hours > DEFAULT_LOOKBACK_HOURS


def test_the_hourly_surfaces_keep_the_measured_default():
    """Widening everything would trade away recency for no reason — the hourly
    surfaces already clear the baseline at 48h, which was itself a measured choice
    (the cron yields ~7 runs/day, not 24)."""
    for name in ("coordinator_quality", "online_quality", "router_efficiency"):
        assert SURFACES[name].lookback_hours == DEFAULT_LOOKBACK_HOURS, name


def test_the_guard_would_reject_the_old_configuration():
    """Guards the guard: the invariant must actually fail on what was shipped."""
    old_points = DEFAULT_LOOKBACK_HOURS / 24.0 * 1.0  # daily writer, 48h window
    assert old_points < MIN_BASELINE + 1


class TestTheWindowIsActuallyUsed:
    """A declared `lookback_hours` that the query ignores fixes nothing.

    This repo has shipped that exact shape before — a type declared and never
    threaded, a metric computed and discarded. The table saying 240h is worthless
    unless the Monitoring query asks for 240h.
    """

    @staticmethod
    def _windows_requested(hours=None):
        """Run the real query loop against a stub client and record the windows."""
        from datetime import datetime

        from src.eval import verify_monitors as vm

        seen: dict[str, float] = {}

        class _Client:
            def list_time_series(self, request=None, **kw):
                req = request or kw
                iv = req["interval"] if isinstance(req, dict) else req.interval
                start = iv["start_time"] if isinstance(iv, dict) else iv.start_time
                end = iv["end_time"] if isinstance(iv, dict) else iv.end_time
                if not isinstance(start, datetime):
                    start = datetime.fromtimestamp(start.timestamp(), tz=UTC)
                    end = datetime.fromtimestamp(end.timestamp(), tz=UTC)
                flt = req["filter"] if isinstance(req, dict) else req.filter
                for key, spec in vm.SURFACES.items():
                    if spec.prefix in flt:
                        seen[key] = round((end - start).total_seconds() / 3600.0)
                return []

        vm._verify_from_monitoring(hours, client=_Client())
        return seen

    def test_each_surface_is_queried_over_its_own_window(self):
        from src.eval.verify_monitors import SURFACES

        seen = self._windows_requested()
        assert seen, "the stub client was never called — the probe is broken"
        for key, window in seen.items():
            assert window == SURFACES[key].lookback_hours, (
                f"{key} was queried over {window}h but declares {SURFACES[key].lookback_hours}h"
            )

    def test_the_daily_surface_really_asks_for_more_than_the_others(self):
        """The whole point, stated as a comparison rather than a constant."""
        seen = self._windows_requested()
        assert seen["router_quality"] > seen["coordinator_quality"]

    def test_an_explicit_override_applies_to_every_surface(self):
        """`--hours 24` is a deliberate narrowing and must not be silently ignored
        for one surface — it is also why the report prints the window per block."""
        seen = self._windows_requested(hours=24)
        assert set(seen.values()) == {24}, seen


class TestTheCliDoesNotForceAnOverride:
    """The per-surface window was inert through the CLI, and the unit tests missed it.

    `main()` set `hours = DEFAULT_LOOKBACK_HOURS` before calling, which is
    indistinguishable from an explicit `--hours 48` — so every surface got 48h and
    the daily one went straight back below MIN_BASELINE. The query-loop tests above
    all passed, because they call `_verify_from_monitoring` directly and never go
    through `main`.

    Caught by running the CLI and reading the output, not by the suite. Hence this
    test: the defaulting lives in one place, and the CLI must not pre-empt it.
    """

    def test_the_cli_block_defaults_to_none_not_to_the_global_window(self):
        import ast
        import pathlib

        src = (
            pathlib.Path(__file__).resolve().parents[1] / "src/eval/verify_monitors.py"
        ).read_text()
        # The CLI is an `if __name__ == "__main__":` block, not a main() function —
        # the first version of this test looked for a FunctionDef and raised
        # StopIteration, passing over the very line it was written to guard.
        block = next(
            n
            for n in ast.parse(src).body
            if isinstance(n, ast.If)
            and ast.unparse(n.test).replace("'", '"') == '__name__ == "__main__"'
        )
        assigns = [
            n
            for n in ast.walk(block)
            if isinstance(n, ast.Assign)
            and any(getattr(t, "id", None) == "hours" for t in n.targets)
        ]
        assert assigns, "the CLI block no longer assigns `hours`"
        first = assigns[0].value
        assert isinstance(first, ast.Constant) and first.value is None, (
            f"the CLI seeds `hours` with {ast.unparse(first)}; that reads as an explicit "
            "--hours override and disables every surface's own window"
        )

    def test_the_entry_point_default_is_none(self):
        import inspect

        from src.eval.verify_monitors import verify_monitor_results

        assert inspect.signature(verify_monitor_results).parameters["hours"].default is None
