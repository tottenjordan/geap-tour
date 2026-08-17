"""Offline tests for the rolling-baseline z-score anomaly detector (no GCP).

Pure stdlib math — deterministic, credential-free. Complements the static-floor
alerts in ``quality_alerts.py``: the baseline answers "has the value moved
anomalously far from its own recent history?", which the absolute floor can't.
"""

import math

from src.eval import baseline as bl


# --------------------------------------------------------------------------- #
# mean / stddev
# --------------------------------------------------------------------------- #
def test_mean_basic():
    assert bl.mean([1.0, 2.0, 3.0]) == 2.0


def test_stddev_sample_ddof1():
    # sample std of [2,4,4,4,5,5,7,9] is 2.138... (ddof=1)
    assert math.isclose(bl.stddev([2, 4, 4, 4, 5, 5, 7, 9]), 2.13809, rel_tol=1e-4)


def test_stddev_zero_for_no_variance_or_singleton():
    assert bl.stddev([3.0, 3.0, 3.0]) == 0.0
    assert bl.stddev([5.0]) == 0.0
    assert bl.stddev([]) == 0.0


# --------------------------------------------------------------------------- #
# zscore
# --------------------------------------------------------------------------- #
def test_zscore_positive_and_negative():
    hist = [1.0, 2.0, 3.0, 4.0, 5.0]  # mean 3, sample std ~1.5811
    assert math.isclose(bl.zscore(6.0, hist), (6.0 - 3.0) / 1.5811388, rel_tol=1e-5)
    assert bl.zscore(0.0, hist) < 0


def test_zscore_none_when_flat_baseline():
    assert bl.zscore(4.0, [3.0, 3.0, 3.0]) is None


# --------------------------------------------------------------------------- #
# detect_regression — history sufficiency
# --------------------------------------------------------------------------- #
def test_detect_regression_insufficient_history():
    out = bl.detect_regression([4.0, 4.1, 4.0], 3.9, direction="LT")
    assert out["status"] == "insufficient_history"
    assert out["is_anomaly"] is False
    assert out["n_baseline"] == 3


def test_detect_regression_no_variance_flat_baseline():
    out = bl.detect_regression([4.0] * 6, 3.0, direction="LT")
    assert out["status"] == "no_variance"
    assert out["is_anomaly"] is False


# --------------------------------------------------------------------------- #
# detect_regression — direction-aware anomaly flagging
# --------------------------------------------------------------------------- #
def test_detect_regression_lt_flags_drop_only():
    # A stable ~4.0 quality series, then a sharp drop -> anomaly for an LT metric.
    hist = [4.0, 4.1, 3.9, 4.0, 4.05, 3.95]
    dropped = bl.detect_regression(hist, 2.5, direction="LT")
    assert dropped["status"] == "ok"
    assert dropped["z"] < 0
    assert dropped["is_anomaly"] is True
    # A spike UP is not a regression for a floor (LT) metric.
    spiked = bl.detect_regression(hist, 5.0, direction="LT")
    assert spiked["is_anomaly"] is False


def test_detect_regression_gt_flags_spike_only():
    # A stable low empty-rate/latency series, then a spike UP -> anomaly for GT.
    hist = [0.02, 0.03, 0.01, 0.02, 0.025, 0.015]
    spiked = bl.detect_regression(hist, 0.5, direction="GT")
    assert spiked["status"] == "ok"
    assert spiked["z"] > 0
    assert spiked["is_anomaly"] is True
    # A drop toward zero is good news for a ceiling (GT) metric, not an anomaly.
    dropped = bl.detect_regression(hist, 0.0, direction="GT")
    assert dropped["is_anomaly"] is False


def test_detect_regression_within_band_is_not_anomaly():
    hist = [4.0, 4.1, 3.9, 4.0, 4.05, 3.95]
    out = bl.detect_regression(hist, 4.02, direction="LT")
    assert out["status"] == "ok"
    assert out["is_anomaly"] is False
    assert abs(out["z"]) < 2.0


def test_detect_regression_custom_z_threshold():
    hist = [4.0, 4.1, 3.9, 4.0, 4.05, 3.95]
    # A modest dip that clears a loose 3.0 threshold but not a tight 1.0 one.
    loose = bl.detect_regression(hist, 3.8, direction="LT", z_threshold=3.0)
    tight = bl.detect_regression(hist, 3.8, direction="LT", z_threshold=1.0)
    assert loose["is_anomaly"] is False
    assert tight["is_anomaly"] is True
