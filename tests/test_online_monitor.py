"""Offline tests for the online quality monitor's pure core + publish (no GCP).

The live-traffic driver (``stream_query``) is not unit-tested — it is a thin
wrapper over a deployed engine, like the standalone judges' ``run_*_eval``. The
scoring core, sampling, aggregation, scaling, and metric emission are fully
covered with an injected ``generate_fn`` and a fake metric client.
"""

from src.eval import online_monitor as om
from src.observability.metrics import MetricsWriter


class FakeMetricClient:
    """Records create_time_series calls; no network."""

    def __init__(self):
        self.calls = []

    def create_time_series(self, name=None, time_series=None):
        self.calls.append((name, list(time_series)))

    def flatten(self):
        out = []
        for _name, ts_list in self.calls:
            out.extend(ts_list)
        return out


def _by_type(client):
    return {ts.metric.type: ts.points[0].value.double_value for ts in client.flatten()}


# --------------------------------------------------------------------------- #
# parse_score
# --------------------------------------------------------------------------- #
def test_parse_score_maps_1_5_to_0_1():
    assert om.parse_score("Score: 5") == 1.0
    assert om.parse_score("Score: 3") == 0.6
    assert om.parse_score("blah\nScore: 1") == 0.2


def test_parse_score_uses_last_match():
    # Judges restate criterion scores before the final verdict.
    assert om.parse_score("Criterion score: 2\nFinal Score: 4") == 0.8


def test_parse_score_none_when_unparseable():
    assert om.parse_score("no number here") is None
    assert om.parse_score("") is None
    assert om.parse_score(None) is None


# --------------------------------------------------------------------------- #
# rubric prompts
# --------------------------------------------------------------------------- #
def test_helpfulness_prompt_embeds_pair_and_score_directive():
    p = om.build_helpfulness_prompt("find flights", "here are 3 flights")
    assert "find flights" in p
    assert "here are 3 flights" in p
    assert "Score: <1-5>" in p


def test_rubric_builders_cover_all_monitored_names():
    assert set(om.RUBRIC_BUILDERS) == {
        "helpfulness",
        "tool_use_accuracy",
        "policy_compliance",
    }


# --------------------------------------------------------------------------- #
# score_interaction
# --------------------------------------------------------------------------- #
def test_score_interaction_scores_every_metric():
    scores = om.score_interaction("p", "r", generate_fn=lambda _prompt: "Score: 4")
    assert scores == {"helpfulness": 0.8, "tool_use_accuracy": 0.8, "policy_compliance": 0.8}


def test_score_interaction_skips_unparseable_metric():
    # helpfulness prompt gets a parseable verdict; the others don't.
    def gen(prompt):
        return "Score: 5" if "HELPFUL" in prompt else "sorry, cannot judge"

    scores = om.score_interaction("p", "r", generate_fn=gen)
    assert scores == {"helpfulness": 1.0}


def test_score_interaction_respects_metric_subset():
    scores = om.score_interaction(
        "p", "r", generate_fn=lambda _p: "Score: 3", metrics=["helpfulness"]
    )
    assert scores == {"helpfulness": 0.6}


# --------------------------------------------------------------------------- #
# sample_interactions
# --------------------------------------------------------------------------- #
def test_sample_rate_one_keeps_all():
    items = [("p", "r")] * 5
    assert om.sample_interactions(items, 1.0) == items


def test_sample_rate_half_takes_every_other():
    items = list(range(6))
    assert om.sample_interactions(items, 0.5) == [0, 2, 4]


def test_sample_rate_zero_or_negative_keeps_none():
    assert om.sample_interactions([1, 2, 3], 0.0) == []
    assert om.sample_interactions([1, 2, 3], -1.0) == []


# --------------------------------------------------------------------------- #
# aggregate_scores
# --------------------------------------------------------------------------- #
def test_aggregate_means_per_metric():
    agg = om.aggregate_scores(
        [
            {"helpfulness": 1.0, "policy_compliance": 0.6},
            {"helpfulness": 0.6},
        ]
    )
    assert agg["scores"]["helpfulness"] == 0.8
    assert agg["scores"]["policy_compliance"] == 0.6
    assert agg["counts"] == {"helpfulness": 2, "policy_compliance": 1}
    assert agg["n_interactions"] == 2


def test_aggregate_empty():
    agg = om.aggregate_scores([])
    assert agg["scores"] == {}
    assert agg["n_interactions"] == 0


def test_aggregate_flags_low_confidence_below_floor():
    # 3 interactions is below the sample floor -> flagged low_confidence.
    agg = om.aggregate_scores([{"helpfulness": 0.8}] * 3)
    assert agg["low_confidence"]["helpfulness"] is True


def test_aggregate_reports_ci_bracketing_the_mean():
    agg = om.aggregate_scores(
        [{"helpfulness": v} for v in (0.2, 0.4, 0.6, 0.8, 1.0, 0.6, 0.4, 0.8)]
    )
    lo, hi = agg["ci"]["helpfulness"]
    assert lo <= agg["scores"]["helpfulness"] <= hi


# --------------------------------------------------------------------------- #
# publish_online_scores
# --------------------------------------------------------------------------- #
def test_publish_scales_0_1_to_1_5_and_tags_online():
    client = FakeMetricClient()
    w = MetricsWriter(project_id="proj-x", client=client)
    published = om.publish_online_scores({"helpfulness": 0.6, "tool_use_accuracy": 1.0}, writer=w)
    assert published == {"helpfulness": 3.0, "tool_use_accuracy": 5.0}
    vals = _by_type(client)
    assert vals["custom.googleapis.com/agent_online_eval/helpfulness"] == 3.0
    assert vals["custom.googleapis.com/agent_online_eval/tool_use_accuracy"] == 5.0
    for ts in client.flatten():
        assert ts.metric.labels["eval_mode"] == "online"


def test_publish_drops_non_monitored_metrics():
    client = FakeMetricClient()
    w = MetricsWriter(project_id="proj-x", client=client)
    published = om.publish_online_scores({"helpfulness": 0.8, "made_up": 0.9}, writer=w)
    assert published == {"helpfulness": 4.0}
    assert "made_up" not in {ts.metric.type.rsplit("/", 1)[-1] for ts in client.flatten()}


def test_publish_extra_labels_merged_without_clobbering_eval_mode():
    client = FakeMetricClient()
    w = MetricsWriter(project_id="proj-x", client=client)
    om.publish_online_scores({"helpfulness": 0.8}, writer=w, extra_labels={"model": "gem"})
    ts = client.flatten()[0]
    assert ts.metric.labels["model"] == "gem"
    assert ts.metric.labels["eval_mode"] == "online"


def test_publish_empty_scores_writes_nothing():
    client = FakeMetricClient()
    w = MetricsWriter(project_id="proj-x", client=client)
    assert om.publish_online_scores({}, writer=w) == {}
    assert client.calls == []


# --------------------------------------------------------------------------- #
# score_and_publish (end-to-end, fakes only)
# --------------------------------------------------------------------------- #
def test_score_and_publish_end_to_end():
    client = FakeMetricClient()
    w = MetricsWriter(project_id="proj-x", client=client)
    pairs = [("find flights", "here are flights"), ("submit expense", "submitted")]
    result = om.score_and_publish(
        pairs,
        generate_fn=lambda _p: "Score: 4",
        writer=w,
    )
    assert result["n_captured"] == 2
    assert result["n_sampled"] == 2
    # every metric == 0.8 -> 4.0 on the 1-5 axis
    assert result["published"] == {
        "helpfulness": 4.0,
        "tool_use_accuracy": 4.0,
        "policy_compliance": 4.0,
    }


def test_score_and_publish_dry_run_skips_write():
    client = FakeMetricClient()
    w = MetricsWriter(project_id="proj-x", client=client)
    result = om.score_and_publish(
        [("p", "r")],
        generate_fn=lambda _p: "Score: 5",
        writer=w,
        dry_run=True,
    )
    assert result["published"] == {}
    assert client.calls == []
    # aggregate is still computed for reporting
    assert result["aggregate"]["scores"]["helpfulness"] == 1.0


# --------------------------------------------------------------------------- #
# infra-empty separation (P2.8): empty 200s are an infra signal, not low quality
# --------------------------------------------------------------------------- #
def test_is_infra_empty_detects_empty_and_error_shaped():
    assert om.is_infra_empty("")
    assert om.is_infra_empty("   \n ")
    assert om.is_infra_empty('{"error": "Failed to parse agent run response"}')
    assert not om.is_infra_empty("here are your flights")


def test_partition_separates_empty_from_real():
    pairs = [("p1", "real answer"), ("p2", ""), ("p3", '{"error": "x"}')]
    real, empty = om.partition_interactions(pairs)
    assert real == [("p1", "real answer")]
    assert len(empty) == 2


def test_score_and_publish_excludes_infra_empty_from_quality_mean():
    client = FakeMetricClient()
    w = MetricsWriter(project_id="proj-x", client=client)
    # 1 real (would score 0.8 -> 4.0) + 2 empty. A judge that scored the empties
    # low would drag the mean; instead they are excluded and counted separately.
    pairs = [("find flights", "here are flights"), ("p2", ""), ("p3", "   ")]
    result = om.score_and_publish(pairs, generate_fn=lambda _p: "Score: 4", writer=w)
    assert result["n_captured"] == 3
    assert result["n_infra_empty"] == 2
    assert result["infra_empty_rate"] == 2 / 3
    # the quality mean reflects ONLY the one real interaction, not the empties
    assert result["aggregate"]["scores"]["helpfulness"] == 0.8
    assert result["aggregate"]["counts"]["helpfulness"] == 1


def test_score_and_publish_publishes_infra_empty_rate_verbatim():
    client = FakeMetricClient()
    w = MetricsWriter(project_id="proj-x", client=client)
    pairs = [("p1", "real"), ("p2", "")]
    result = om.score_and_publish(pairs, generate_fn=lambda _p: "Score: 5", writer=w)
    by_type = _by_type(client)
    # infra_empty_rate lands on the online family, written verbatim (0-1), NOT
    # scaled to the 1-5 quality axis.
    assert by_type["custom.googleapis.com/agent_online_eval/infra_empty_rate"] == 0.5
    assert result["infra_published"] == {"infra_empty_rate": 0.5}


def test_score_and_publish_all_empty_publishes_no_quality_but_flags_infra():
    client = FakeMetricClient()
    w = MetricsWriter(project_id="proj-x", client=client)
    result = om.score_and_publish(
        [("p1", ""), ("p2", '{"error": "x"}')], generate_fn=lambda _p: "Score: 5", writer=w
    )
    assert result["published"] == {}  # nothing real to score
    assert result["infra_empty_rate"] == 1.0
    assert _by_type(client)["custom.googleapis.com/agent_online_eval/infra_empty_rate"] == 1.0


# --------------------------------------------------------------------------- #
# Tool-call faithfulness on the online surface (trajectory-retaining capture +
# grounded judge → agent_online_eval/tool_faithfulness)
# --------------------------------------------------------------------------- #
def _fc_event(name, args=None):
    return {
        "author": "model",
        "content": {"parts": [{"function_call": {"name": name, "args": args or {}}}]},
    }


def _text_event(text):
    return {"author": "model", "content": {"parts": [{"text": text}]}}


class _FakeAgent:
    """Deployed-engine stand-in: create_session + stream_query(events)."""

    def __init__(self, events_by_prompt):
        self._events_by_prompt = events_by_prompt
        self.messages = []

    def create_session(self, *, user_id):
        return {"id": "sess-1"}

    def stream_query(self, *, user_id, session_id, message):
        self.messages.append(message)
        yield from self._events_by_prompt.get(message, [])


def test_capture_live_faithfulness_yields_triples():
    agent = _FakeAgent(
        {
            "Book FL001": [
                _fc_event("book_flight", {"flight_id": "FL001"}),
                _text_event("Booked."),
            ],
        }
    )
    triples = om.capture_live_faithfulness(agent, ["Book FL001"])
    assert len(triples) == 1
    assert triples[0]["prompt"] == "Book FL001"
    assert triples[0]["response"] == "Booked."
    assert [c["tool_name"] for c in triples[0]["actual_trajectory"]] == ["book_flight"]


def test_score_and_publish_faithfulness_publishes_online_series():
    client = FakeMetricClient()
    w = MetricsWriter(project_id="proj-x", client=client)
    # Response claims a booking but the trajectory is empty → hallucinated.
    triples = [{"prompt": "Book FL001", "response": "I booked FL001.", "actual_trajectory": []}]
    result = om.score_and_publish_faithfulness(
        triples, generate_fn=lambda _p: "Hallucinated: book_flight\nScore: 2", writer=w
    )
    assert result["published"] == {"tool_faithfulness": 2.0}  # 0.4 * 5
    vals = _by_type(client)
    assert vals["custom.googleapis.com/agent_online_eval/tool_faithfulness"] == 2.0
    for ts in client.flatten():
        assert ts.metric.labels["eval_mode"] == "online"
    assert result["result"]["flagged"][0]["hallucinated"] == ["book_flight"]


def test_score_and_publish_faithfulness_dry_run_writes_nothing():
    client = FakeMetricClient()
    w = MetricsWriter(project_id="proj-x", client=client)
    triples = [{"prompt": "p", "response": "I booked it", "actual_trajectory": []}]
    result = om.score_and_publish_faithfulness(
        triples, generate_fn=lambda _p: "Score: 2", writer=w, dry_run=True
    )
    assert result["published"] == {}
    assert client.calls == []
    assert result["result"]["score"] == 0.4  # still computed for reporting


def test_score_and_publish_faithfulness_excludes_infra_empty():
    triples = [
        {"prompt": "p1", "response": "I booked FL001", "actual_trajectory": []},
        {"prompt": "p2", "response": "", "actual_trajectory": []},
    ]
    result = om.score_and_publish_faithfulness(
        triples, generate_fn=lambda _p: "Score: 4", dry_run=True
    )
    # only the one real response is judged; the empty-at-200 is excluded.
    assert result["result"]["n_scored"] == 1
    assert result["n_infra_empty"] == 1


class _SkewAgent:
    """Deployed-engine stand-in whose SDK stream raises the array-parse skew.

    Mirrors the live symptom: google-api-core's array-only REST parser raises
    ``ValueError: Can only parse array of JSON objects`` on an engine that streams
    NDJSON via ``:streamQuery?alt=sse``. Carries a ``resource_name`` so the raw
    fallback can address the engine directly.
    """

    def __init__(self, resource_name="projects/p/locations/us-central1/reasoningEngines/123"):
        self.resource_name = resource_name

    def create_session(self, *, user_id):
        return {"id": "sess-1"}

    def stream_query(self, *, user_id, session_id, message):
        raise ValueError("Can only parse array of JSON objects, instead got {")
        yield  # pragma: no cover - unreachable, marks this a generator


def test_capture_live_interactions_falls_back_to_raw_on_sse_skew(monkeypatch):
    agent = _SkewAgent()
    seen = {}

    def fake_capture_pairs(resource_name, prompts, user_id="online-monitor-user", **kw):
        seen["resource_name"] = resource_name
        seen["prompts"] = list(prompts)
        return [(p, "raw text") for p in prompts]

    monkeypatch.setattr(om.raw_stream, "capture_pairs", fake_capture_pairs)
    pairs = om.capture_live_interactions(agent, ["hi"])
    assert pairs == [("hi", "raw text")]
    assert seen["resource_name"] == "projects/p/locations/us-central1/reasoningEngines/123"


def test_capture_live_faithfulness_falls_back_to_raw_on_sse_skew(monkeypatch):
    agent = _SkewAgent()

    def fake_capture_triples(resource_name, prompts, user_id="online-monitor-user", **kw):
        return [
            {"prompt": p, "response": "raw", "actual_trajectory": [{"tool_name": "book_flight"}]}
            for p in prompts
        ]

    monkeypatch.setattr(om.raw_stream, "capture_triples", fake_capture_triples)
    triples = om.capture_live_faithfulness(agent, ["Book FL001"])
    assert triples[0]["response"] == "raw"
    assert triples[0]["actual_trajectory"][0]["tool_name"] == "book_flight"


def test_capture_live_interactions_reraises_unrelated_valueerror(monkeypatch):
    class _BoomAgent(_SkewAgent):
        def stream_query(self, *, user_id, session_id, message):
            raise ValueError("something else entirely")
            yield  # pragma: no cover

    called = {"raw": False}

    def fake_capture_pairs(*a, **k):
        called["raw"] = True
        return []

    monkeypatch.setattr(om.raw_stream, "capture_pairs", fake_capture_pairs)
    import pytest

    with pytest.raises(ValueError, match="something else entirely"):
        om.capture_live_interactions(_BoomAgent(), ["hi"])
    assert called["raw"] is False  # unrelated errors are NOT masked by the fallback


def test_run_online_faithfulness_with_fakes():
    client = FakeMetricClient()
    w = MetricsWriter(project_id="proj-x", client=client)
    agent = _FakeAgent(
        {"Book FL001": [_fc_event("book_flight", {"flight_id": "FL001"}), _text_event("Booked.")]}
    )
    result = om.run_online_faithfulness(
        agent=agent,
        prompts=["Book FL001"],
        generate_fn=lambda _p: "Hallucinated: NONE\nScore: 5",
        writer=w,
    )
    assert result["published"] == {"tool_faithfulness": 5.0}
    assert result["n_captured"] == 1


# --------------------------------------------------------------------------- #
# Judge panel (diverse multi-model) — the online surface is no longer a single
# autorater's unchecked verdict (roadmap P1.4 wired into online eval).
# --------------------------------------------------------------------------- #
def test_score_interaction_panel_medians_across_judges():
    # three judges score 3, 5, 4 on every rubric → median 4 → 0.8
    judges = [lambda _p: "Score: 3", lambda _p: "Score: 5", lambda _p: "Score: 4"]
    medians, per_judge = om.score_interaction_panel("p", "r", judges)
    assert medians == {"helpfulness": 0.8, "tool_use_accuracy": 0.8, "policy_compliance": 0.8}
    # per-judge rows preserved (in panel order) for inter-rater reliability
    assert per_judge["helpfulness"] == [0.6, 1.0, 0.8]
    assert all(len(row) == 3 for row in per_judge.values())


def test_score_interaction_panel_uses_robust_median_not_mean():
    # one contrarian judge (Score 1) cannot swing the median off 4/5=0.8
    judges = [lambda _p: "Score: 4", lambda _p: "Score: 4", lambda _p: "Score: 1"]
    medians, _ = om.score_interaction_panel("p", "r", judges)
    assert medians["helpfulness"] == 0.8


def test_score_interaction_panel_drops_metric_when_all_judges_unparseable():
    judges = [lambda _p: "no verdict", lambda _p: "still nothing"]
    medians, per_judge = om.score_interaction_panel("p", "r", judges)
    assert medians == {}
    assert per_judge["helpfulness"] == [None, None]


def test_score_and_publish_panel_publishes_medians_and_reliability():
    client = FakeMetricClient()
    w = MetricsWriter(project_id="proj-x", client=client)
    pairs = [("p1", "r1"), ("p2", "r2")]
    judges = [lambda _p: "Score: 4", lambda _p: "Score: 4", lambda _p: "Score: 5"]
    result = om.score_and_publish(pairs, judges=judges, writer=w)
    # per-item median of [0.8, 0.8, 1.0] = 0.8 → 4.0 on the 1-5 axis
    assert result["published"]["helpfulness"] == 4.0
    agg = result["aggregate"]
    assert "reliability" in agg
    rel = agg["reliability"]["helpfulness"]
    assert rel["n_judges"] == 3
    # per-item spread = 1.0 - 0.8 = 0.2, constant across the two items
    assert abs(rel["mean_spread"] - 0.2) < 1e-9
    assert isinstance(rel["alpha"], float)


def test_score_and_publish_panel_dry_run_writes_nothing():
    client = FakeMetricClient()
    w = MetricsWriter(project_id="proj-x", client=client)
    judges = [lambda _p: "Score: 5"]
    result = om.score_and_publish([("p", "r")], judges=judges, writer=w, dry_run=True)
    assert client.calls == []
    assert result["published"] == {}
    assert result["aggregate"]["scores"]["helpfulness"] == 1.0
    assert "reliability" in result["aggregate"]


def test_score_and_publish_requires_generate_fn_or_judges():
    import pytest

    with pytest.raises(ValueError, match="generate_fn or judges"):
        om.score_and_publish([("p", "r")])


def test_run_online_monitor_panel_uses_injected_judges_and_agent():
    client = FakeMetricClient()
    w = MetricsWriter(project_id="proj-x", client=client)
    agent = _FakeAgent({"hi": [_text_event("hello there")], "bye": [_text_event("goodbye")]})
    judges = [lambda _p: "Score: 4", lambda _p: "Score: 5"]
    result = om.run_online_monitor(agent=agent, judges=judges, prompts=["hi", "bye"], writer=w)
    # per-item median of [0.8, 1.0] = 0.9 → 4.5 on the 1-5 axis
    assert result["published"]["helpfulness"] == 4.5
    assert result["aggregate"]["reliability"]["helpfulness"]["n_judges"] == 2
