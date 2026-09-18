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


class WinRateSignificance(TypedDict):
    """A pairwise win-rate with the sign test and interval that qualify it.

    ``decisive`` is the denominator, not ``wins + losses + ties``: ties are excluded
    upstream, and reporting a win-rate over all cases would dilute exactly the
    effect the test is looking for. Every boundary decision in the router
    (``flash beat lite 18-1``, ``sonnet beats pro 17-1``) is this record.
    """

    wins: int
    losses: int
    decisive: int
    win_rate_decisive: float
    p_value: float
    significant: bool
    ci_low: float
    ci_high: float
    alpha: float


class PowerReport(TypedDict):
    """Is a proportion verdict supported by its sample, and if not, what would be.

    ``verdict`` is three-valued — ``"above"`` / ``"below"`` / ``"inconclusive"`` — and
    ``needed_n`` names the sample size that would resolve it (``None`` when already
    resolved or unreachable). Collapsing this to a bool is the false-precision
    failure the three-valued gates and the n=3 noise retraction both came from.
    """

    n: int
    rate: float
    ci: tuple[float, float]
    threshold: float
    resolved: bool
    needed_n: int | None
    verdict: Literal["above", "below", "inconclusive"]


class MeanPowerReport(TypedDict):
    """The same question asked of a MEAN rather than a proportion.

    Kept separate from :class:`PowerReport` on purpose: they answer different
    questions and their ``verdict`` vocabularies differ
    (``healthy``/``breached``/``inconclusive`` vs ``above``/``below``). A monitored
    gauge alerts on its value, so the claim under test is about the mean; framing it
    as a good-share also sets an unreachable bar, under which a perfectly healthy
    24-point series reads as underpowered and every alert is suppressed.

    ``needed_n`` is absent here — there is no closed form for the bootstrap.
    """

    n: int
    mean: float
    ci: tuple[float, float]
    threshold: float
    resolved: bool
    verdict: Literal["healthy", "breached", "inconclusive"]


class RegressionCheck(TypedDict):
    """A rolling-baseline z-score verdict, with the reason it may not have one.

    ``status`` carries why: ``insufficient_history`` (fewer than ``min_baseline``
    points), ``no_variance`` (a flat baseline leaves z undefined), or ``ok``. The
    statistical keys are ``NotRequired`` because the first two states genuinely have
    none — and that is the point. ``is_anomaly=False`` alongside
    ``status="insufficient_history"`` is *not* a clean bill of health, and a type
    that forced a ``baseline_mean`` into that branch would invite reading one.
    """

    status: Literal["insufficient_history", "no_variance", "ok"]
    is_anomaly: bool
    n_baseline: int
    min_baseline: NotRequired[int]
    baseline_mean: NotRequired[float]
    baseline_std: NotRequired[float]
    z: NotRequired[float | None]
    current: NotRequired[float]
    direction: NotRequired[str]
    z_threshold: NotRequired[float]


class CostSummary(TypedDict):
    """Measured spend for one model over its usage records.

    ``mean_usd_per_request`` is 0.0 when ``n_requests`` is 0 — a zero that means "no
    data", not "free". The bake-off reports an honest ``n/a`` rather than a fake $0
    for exactly this reason; the two keys have to be read together.
    """

    model: str
    n_requests: int
    total_usd: float
    mean_usd_per_request: float


class ClassifierAccuracy(TypedDict):
    """How well the complexity classifier bands prompts.

    Graded against **fixed reference bands**, deliberately not the tunable
    ``COMPLEXITY_LOW``/``COMPLEXITY_HIGH`` cut-points. That is the whole design:
    bucketing by the boundaries made the score move whenever *routing* was retuned,
    and it did — the same 40 prompts and the same classifier read 50% and then 82.5%
    purely because ``COMPLEXITY_LOW`` went 0.44 -> 0.25.

    **``accuracy_pct`` is a formatted STRING** — ``"82.5%"``, not ``82.5``. The
    alerting series of almost the same name, ``agent_router/classifier_accuracy_pct``,
    is computed from the ``accuracy`` float instead
    (``publish_router_efficiency.py``), and ``pipelines/components.py`` strips the
    ``%`` before logging it. Two keys, one obvious-looking name, different types and
    different consumers — declaring the type is how that stops being something you
    have to already know. (Typed as ``float`` on the first pass here, from the name;
    ``ty`` rejected it against the real producer.)
    """

    accuracy: float
    accuracy_pct: str
    correct: int
    total_cases: int
    avg_latency_ms: float
    confusion_matrix: dict
    per_case: list[dict]


class CostEfficiency(TypedDict):
    """Routed spend against the all-Opus counterfactual.

    ``savings_pct`` publishes to ``agent_router/cost_savings_pct``. Note it **rises**
    when routing collapses onto the cheapest tier — 93.1% to ~99.6% — so this record
    looks its best exactly when the router has stopped routing. That blind spot is
    why ``lite_tier_pct``/``tiers_used`` exist; nothing in this type can see it.
    """

    routed_cost_usd: float
    all_opus_cost_usd: float
    savings_pct: float
    total_prompts: int
    per_case: list[dict]


class PairwiseAggregate(TypedDict):
    """Win/tie rates for a side-by-side, with the significance test attached.

    ``significance`` is a required key, not an optional extra: a raw win-rate over a
    handful of cases reads like a result, and the whole reason this repo hand-rolled
    a pairwise judge was to be able to say ``18-1, p=0.0001`` rather than "candidate
    looks better". The rates and the test travel together or neither is trustworthy.
    """

    n_cases: int
    win_rate_candidate: float
    win_rate_baseline: float
    tie_rate: float
    significance: WinRateSignificance


class PairwiseResult(PairwiseAggregate):
    """A full side-by-side run: the aggregate, plus what produced it.

    Separate from :class:`PairwiseAggregate` because the aggregate is a pure
    function of the choices while these four keys are run provenance. ``config`` and
    the two engine ids are what make a win-rate reproducible — a 62% with no record
    of the judge model, the flip setting or which engines were compared is a number
    nobody can re-derive.
    """

    per_case: list[dict]
    config: dict
    baseline_engine: str | None
    candidate_engine: str | None


class TrajectoryCapture(TypedDict):
    """One prompt, the visible answer, and the tools actually executed.

    Both halves in one record is the point. ``tool_use_judge`` scores only
    ``(prompt, response)`` because ``run_inference`` yields text and no trajectory,
    which is precisely the gap that lets a reply claim "I booked FL001" with no
    booking call behind it. Faithfulness needs the pair.
    """

    prompt: str
    response: str
    actual_trajectory: list[dict]


class QueryResult(TypedDict):
    """What the SDK's ``EvalTask`` runnable hands back for one prompt.

    Distinct from :class:`TrajectoryCapture` by one key: the SDK already knows the
    prompt it passed in, so echoing it would be the runnable asserting an input
    rather than reporting an output.
    """

    response: str
    predicted_trajectory: list[dict]


class FaithfulnessScores(TypedDict):
    """Whether the agent's claims about its actions match the tools it ran.

    ``flagged`` names the fabricated actions rather than only counting them — a
    score of 2.0 with no names is unactionable, and naming them is what let the
    synthetic-fabrication validation confirm the judge catches
    ``book_flight``/``submit_expense``/``book_hotel`` specifically. Each entry is a
    ``{prompt, hallucinated, score}`` record, not a bare action name: which prompt
    provoked the fabrication is half the diagnosis.

    ``per_case_scores`` is a list of the parsed **scores**, despite the name — only
    the cases that parsed, so it is shorter than ``n_total`` whenever a verdict was
    unreadable. Both element types were guessed wrong on the first pass here and
    corrected by wiring them to the producer.
    """

    score: float | None
    n_scored: int
    n_total: int
    flagged: list[dict]
    per_case_scores: list[float]


class TrajectoryEvalResult(TypedDict):
    """Deterministic trajectory scoring, with the turns it could not score.

    ``empty_trajectories`` is separate from ``scored_cases`` because a turn that
    called no tool is an infra or prompting outcome, not a bad trajectory. Averaging
    it in as a zero is the same mistake the multi-turn rubrics made before
    ``partition_empty_conversations``.
    """

    metrics: dict
    scored_cases: int
    empty_trajectories: int


class DatasetDescription(TypedDict):
    """A dataset's size and content hash.

    An eval score only means something relative to its dataset, and ``checksum`` is
    what makes "the score moved" distinguishable from "the questions changed".
    """

    n_cases: int
    checksum: str


class HealthCheckResult(TypedDict):
    """A flakiness probe run: the raw probes, their rates, and the verdict.

    All three are kept rather than just the verdict, because a verdict of
    INCONCLUSIVE is only interpretable next to the n and interval that produced it.
    """

    engine: str
    results: list[dict]
    summary: RateSummary
    verdict: HealthVerdict
