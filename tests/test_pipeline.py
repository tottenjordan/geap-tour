def test_components_are_kfp():
    from src.pipelines import components as c

    for name in (
        "resolve_agent", "generate_traffic", "batch_eval",
        "simulated_eval", "complexity_eval", "monitor_verify", "report", "cleanup",
    ):
        comp = getattr(c, name)
        assert hasattr(comp, "component_spec"), f"{name} is not a KFP component"
