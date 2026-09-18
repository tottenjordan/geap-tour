"""Declared shapes for the eval records that cross module boundaries.

``src/eval`` returns 91 record-shaped dicts, and before this module the repo
declared exactly one ``TypedDict``. A dict key has no type, so ``ty`` cannot check
it and a wrong assumption about a shape survives until something calls the code for
real. On 2026-09-18 that happened twice in one day:

* :mod:`src.eval.publish_router_quality` assumed the batch eval's ``metrics`` values
  were floats. They are ``{"score", "threshold", "passed"}`` records. Its unit tests
  passed — they were written against the *assumed* shape — and the first **live**
  call raised ``TypeError: float() argument must be ... not 'dict'`` after a
  billable inference run.
* A hand-built verdict omitted ``"status"`` and ``format_report`` raised
  ``KeyError``.

Both become static errors here.

**Scope is deliberately narrow.** Only records that one module produces and another
consumes by key are declared. The other ~85 dict returns are left alone: they have
never failed, and converting them would be churn without evidence. If this file
grows past a handful of types, that tradeoff has been forgotten —
``tests/test_eval_types.py`` asserts the ceiling.
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


class MetricDetail(TypedDict):
    """One rubric metric's result, as ``_run_single_agent_eval`` stores it.

    Built at ``multi_agent_batch_eval.py`` and read by every publisher. The
    ``score`` is on the service's native **0-1** axis; publishers scale it to 1-5.
    Consuming this as a bare float is the exact mistake that reached a live call.

    ``low_confidence`` is optional because ``_annotate_low_confidence`` stamps it
    after construction — requiring it would make the producer's own intermediate
    state a type error.
    """

    score: float
    threshold: float
    passed: bool
    low_confidence: NotRequired[bool]


class HealthVerdict(TypedDict):
    """A three-valued engine-health decision.

    ``status`` is a ``Literal``, not a ``str``, because ``"Inconclusive"`` and
    ``"INCONCLUSIVE"`` are both valid strings and only one is correct — that typo
    class is precisely what this file exists to catch.

    ``passed`` and ``status`` are **not** redundant: ``INCONCLUSIVE`` deliberately
    carries ``passed=True`` so an uncertain reading does not red a gate, while the
    status keeps the display honest. Collapsing them was a real bug (#156).
    """

    passed: bool
    status: Literal["PASS", "FAIL", "INCONCLUSIVE"]
    threshold: float
    reason: str


class RateSummary(TypedDict):
    """An empty-rate measurement and the interval that qualifies it.

    The interval is a required key, not an optional extra: a rate reported without
    one invites a decision the sample size cannot support, which is the failure the
    three-valued verdict and the metric-noise retraction both came from. Keeping
    them in one type means a producer cannot emit the point estimate alone.

    Keys beyond these four (``counts``, ``by_tier``, latency percentiles) are
    produced by ``verify_router_health.summarize`` and are not declared here —
    nothing outside that module reads them by key.
    """

    n: int
    silent_empty: int
    empty_rate: float
    empty_rate_ci: tuple[float, float]


class BatchResult(TypedDict):
    """What ``_run_single_agent_eval`` returns, as its consumers read it.

    **This is the type that does the work.** Declaring :class:`MetricDetail` alone
    was decorative: consumers accepted ``Mapping | None``, so ``result["metrics"]``
    was ``Any`` and ``float(value)`` type-checked fine — the original bug still
    passed ``ty``. The record has to be reachable *through* the container, or the
    type is lost at the entry point.

    Keys are optional because the producer has three early returns (``SKIPPED``
    when every response was empty, ``FAILED`` with an ``error``, and the scored
    path), and each emits a different subset.
    """

    agent: NotRequired[str]
    status: NotRequired[str]
    metrics: NotRequired[dict[str, MetricDetail]]
    error: NotRequired[str]
    reason: NotRequired[str]
    test_cases: NotRequired[int]
    inference_seconds: NotRequired[float]
    empty_responses: NotRequired[int]
    empty_rate: NotRequired[float]
    tool_call_items: NotRequired[int]
    summary_raw: NotRequired[dict]
    evaluation_run_name: NotRequired[str | None]
    item_count: NotRequired[int]
    items: NotRequired[list[dict]]


#: Metric name -> published value on the 1-5 axis. An alias rather than a
#: ``TypedDict`` because the keys are metric names, not a fixed record.
PublishedScores = dict[str, float]
