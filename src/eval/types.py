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

    ``silent_empty`` and ``labelled_failure`` are separate on purpose. A labelled
    throttle is a failed turn but not a *silent* one, and conflating them would make
    the retry wrapper's entire contribution invisible.

    **This type was decorative for its first two hours.** It shipped declaring four
    keys, matched no producer (``_rates`` emits six), and had zero usages outside its
    own declaration and a test that hand-built one — the exact failure the module
    docstring warns about, committed alongside the warning. It is now what
    ``verify_router_health._rates`` returns and what the verdict functions accept.
    """

    n: int
    silent_empty: int
    labelled_failure: int
    empty_rate: float
    empty_rate_ci: tuple[float, float]
    full_rate: float
    # Added by `summarize` after `_rates` builds the base, hence NotRequired: the
    # per-tier breakdown is a RateSummary of the same shape, one level down.
    counts: NotRequired[dict[str, int]]
    skipped: NotRequired[int]
    p50_latency_s: NotRequired[float]
    p95_latency_s: NotRequired[float]
    by_tier: NotRequired[dict[str, RateSummary]]


class JudgeScore(TypedDict):
    """A pointwise judge's mean score over a set of pairs.

    ``policy_judge.score_pairs`` and ``tool_use_judge.score_pairs`` returned this
    exact shape independently; one type now says they are the same contract.

    ``score`` is ``None``, never ``0.0``, when nothing parsed — an unparseable
    verdict is dropped from the average rather than counted as a failure, so a run
    where every verdict was garbage must not read as a perfect zero.
    """

    score: float | None
    n_scored: int
    n_total: int


class PanelScore(JudgeScore):
    """:class:`JudgeScore` plus the inter-rater agreement behind it.

    Separate from ``JudgeScore`` rather than a ``NotRequired`` field on it: a panel
    score without its reliability is not the same claim as a single judge's score,
    and the type should not let one be passed where the other is read.
    """

    reliability: PanelReliability


class PanelReliability(TypedDict):
    """Krippendorff alpha and spread across a judge panel.

    ``alpha`` is ``None`` when it cannot be computed (fewer than two judges scored
    an item), which is different from an alpha of 0 — no agreement measured versus
    no agreement found.
    """

    alpha: float | None
    mean_spread: float
    n_items: int
    n_judges: int


class PanelVerdict(TypedDict):
    """One item scored by the whole panel, before aggregation.

    ``median`` rather than mean is the aggregation on purpose — it is what keeps a
    single miscalibrated autorater from deciding a verdict.
    """

    median: float | None
    # A LIST, positional by judge index — not a dict keyed by judge name. Krippendorff
    # alpha reads these rows positionally, so judge i must stay column i; a dict would
    # make that ordering implicit and reorderable. (Declared as a dict on the first
    # pass of this conversion, from memory rather than from the code — caught by
    # wiring it to the real producer, which is the whole argument for wiring.)
    per_judge: list[float | None]
    spread: float | None
    n_valid: int


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
