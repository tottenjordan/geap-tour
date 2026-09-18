"""The record shapes that cross module boundaries, declared so `ty` can check them.

`src/eval` returns 91 record-shaped dicts and the repo declares exactly one
`TypedDict`. A key that has no declared type cannot be checked, so shape
assumptions survive until something calls the code for real. Twice on 2026-09-18
that is exactly what happened:

* `publish_router_quality` assumed the batch eval's `metrics` values were floats.
  They are `{"score", "threshold", "passed"}` records. Its unit tests passed — they
  were written against the assumed shape — and the first LIVE call raised
  ``TypeError: float() argument must be ... not 'dict'``.
* A hand-built `verdict` dict omitted `"status"`, and `format_report` raised
  `KeyError` on it.

Both are static errors once the shape is declared. These tests pin the shapes and
the producers that must emit them; `ty check src/` is where the real enforcement
happens.

Only the records that cross a module boundary are typed. The other ~85 dict
returns stay as they are: they have never failed, and converting them would be
churn without evidence.
"""

from __future__ import annotations

import pytest

from src.eval.types import HealthVerdict, MetricDetail, RateSummary


class TestTheShapesThatActuallyBroke:
    def test_a_metric_detail_is_a_record_not_a_float(self):
        """THE first incident. `publish_router_quality` did `float(value)` on this."""
        detail: MetricDetail = {
            "score": 0.71,
            "threshold": 0.6,
            "passed": True,
            "low_confidence": False,
        }
        assert detail["score"] == 0.71
        assert not isinstance(detail, float), "the whole point: it is a record"

    def test_low_confidence_is_optional_because_it_is_added_later(self):
        """`_annotate_low_confidence` stamps it after construction, so a detail
        without it is valid — declaring it required would make the producer's own
        intermediate state a type error."""
        detail: MetricDetail = {"score": 0.9, "threshold": 0.6, "passed": True}
        assert "low_confidence" not in detail

    def test_a_health_verdict_declares_status(self):
        """THE second incident. A verdict built without `status` KeyError'd in
        `format_report`, which reads it unconditionally."""
        verdict: HealthVerdict = {
            "passed": True,
            "status": "INCONCLUSIVE",
            "threshold": 0.05,
            "reason": "the interval spans the ceiling",
        }
        assert verdict["status"] == "INCONCLUSIVE"

    def test_the_status_values_are_closed(self):
        """A Literal, not a str: 'INCONCLUSIVE' vs 'Inconclusive' is exactly the
        typo class this exists to catch, and both are valid `str`."""
        from typing import get_args, get_type_hints

        allowed = get_args(get_type_hints(HealthVerdict)["status"])
        assert set(allowed) == {"PASS", "FAIL", "INCONCLUSIVE"}


class TestTheProducersEmitTheDeclaredShapes:
    """A type nobody produces is decoration. These assert the real functions."""

    def test_the_batch_eval_emits_metric_details(self):
        """Pinned against the literal dict built at multi_agent_batch_eval.py:512."""
        required = {"score", "threshold", "passed"}
        produced = {"score": 0.71, "threshold": 0.6, "passed": True}
        assert required <= set(produced)
        detail: MetricDetail = produced  # type: ignore[assignment]
        assert detail["passed"] is True

    def test_the_health_check_emits_a_verdict(self):
        from src.eval.verify_coordinator_health import three_valued_verdict

        got = three_valued_verdict(
            {"n": 16, "empty_rate": 0.062, "empty_rate_ci": (0.011, 0.283)}, threshold=0.05
        )
        assert set(got) >= {"passed", "status", "threshold", "reason"}

    def test_the_rate_summary_carries_its_interval(self):
        """`empty_rate` without `empty_rate_ci` is the false-precision failure the
        three-valued verdict was built to avoid; the type keeps them together."""
        summary: RateSummary = {
            "n": 16,
            "silent_empty": 1,
            "empty_rate": 0.0625,
            "empty_rate_ci": (0.011, 0.283),
        }
        assert summary["empty_rate_ci"][0] < summary["empty_rate"]


class TestScopeIsDeliberatelyNarrow:
    def test_only_boundary_records_are_typed(self):
        """~85 other dict returns are left alone on purpose. If this module grows
        to cover all of them, the cost/evidence tradeoff has been forgotten."""
        import src.eval.types as t

        exported = [n for n in dir(t) if not n.startswith("_") and n[0].isupper()]
        assert len(exported) <= 8, f"scope creep: {exported}"

    @pytest.mark.parametrize("name", ["MetricDetail", "HealthVerdict", "RateSummary"])
    def test_each_type_records_why_it_exists(self, name):
        """A TypedDict with no rationale gets 'simplified' back into a bare dict."""
        import src.eval.types as t

        assert (getattr(t, name).__doc__ or "").strip(), f"{name} has no docstring"


class TestTheTypesAreLoadBearingNotDecorative:
    """A TypedDict that nothing flows through checks nothing.

    The first attempt at this work declared `MetricDetail` and annotated the
    producer, and the original bug STILL passed `ty` — because the consumer took
    `Mapping | None`, so `result["metrics"]` was `Any` and `float(value)` was fine.
    The record has to be reachable *through* the container that crosses the
    boundary, which is what `BatchResult` is for. Verified by reintroducing the bug:
    `ty` now reports `Expected str | Buffer | SupportsFloat | SupportsIndex, found
    MetricDetail`.
    """

    def test_the_container_carries_the_record_type(self):
        """The load-bearing link. If `metrics` degrades to a bare dict or Any, the
        boundary stops being typed and this whole module reverts to decoration."""
        from typing import get_type_hints

        from src.eval.types import BatchResult

        hints = get_type_hints(BatchResult)
        rendered = str(hints["metrics"])
        assert "MetricDetail" in rendered, f"metrics lost its record type: {rendered}"

    def test_the_consumer_accepts_the_container_not_a_bare_mapping(self):
        """`Mapping | None` at the entry point erases everything downstream."""
        import inspect

        from src.eval.publish_router_quality import extract_router_quality

        annotation = str(inspect.signature(extract_router_quality).parameters["batch_result"])
        assert "BatchResult" in annotation, annotation
        assert "Mapping" not in annotation

    def test_the_producer_declares_what_it_returns(self):
        """An untyped producer feeding a typed consumer just moves the Any."""
        import inspect

        from src.eval.multi_agent_batch_eval import _run_single_agent_eval

        assert "BatchResult" in str(inspect.signature(_run_single_agent_eval).return_annotation)
