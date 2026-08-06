def test_components_are_kfp():
    from src.pipelines import components as c

    for name in (
        "resolve_agent", "generate_traffic", "batch_eval",
        "simulated_eval", "complexity_eval", "monitor_verify", "report", "cleanup",
    ):
        comp = getattr(c, name)
        assert hasattr(comp, "component_spec"), f"{name} is not a KFP component"


def test_pipeline_compiles(tmp_path):
    from kfp import compiler

    from src.pipelines.eval_pipeline import eval_pipeline

    out = tmp_path / "pipeline.json"
    compiler.Compiler().compile(eval_pipeline, str(out))
    assert out.exists() and out.stat().st_size > 0
