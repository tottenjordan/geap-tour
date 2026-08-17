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
