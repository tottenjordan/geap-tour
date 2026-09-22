class TestTheMetricNameIsReadable:
    """A demo cell printed `<...LazyLoadedPrebuiltMetric object at 0x7f...>`.

    The header read `--- Clusters for <object at 0x...> ---` in front of an audience.
    The SDK's prebuilt metrics are `LazyLoadedPrebuiltMetric`, which has `.name` but
    no `.value`, so a `.value`-first lookup fell through to `str(metric)`.
    """

    def test_a_prebuilt_metric_renders_as_its_name(self):
        from src.eval.failure_clusters import EVAL_METRICS, _metric_label

        for m in EVAL_METRICS:
            label = _metric_label(m)
            assert "object at 0x" not in label, f"unreadable metric label: {label}"
            assert label.isupper() or "_" in label, label

    def test_it_falls_back_rather_than_raising(self):
        """An SDK that renames the attribute must degrade, not crash a demo cell."""
        from src.eval.failure_clusters import _metric_label

        assert _metric_label(object()) is not None
