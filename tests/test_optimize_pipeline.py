"""Optimize pipeline compiles and exposes its params (no GCP, no GEPA run)."""

import inspect


def test_optimize_agent_is_kfp_component():
    from src.pipelines import components as c

    assert hasattr(c.optimize_agent, "component_spec")


def test_optimize_agent_accepts_expected_params():
    from src.pipelines import components as c

    params = inspect.signature(c.optimize_agent.python_func).parameters
    for name in (
        "agent_opt_module",
        "sampler_config_path",
        "optimizer_config_path",
        "experiment_id",
        "agent_tag",
    ):
        assert name in params


def test_optimize_pipeline_compiles(tmp_path):
    from kfp import compiler

    from src.pipelines.optimize_pipeline import optimize_pipeline

    out = tmp_path / "optimize_pipeline.json"
    compiler.Compiler().compile(optimize_pipeline, str(out))
    assert out.exists() and out.stat().st_size > 0


def test_optimize_pipeline_exposes_params(tmp_path):
    from kfp import compiler

    from src.pipelines.optimize_pipeline import optimize_pipeline

    out = tmp_path / "optimize_pipeline.json"
    compiler.Compiler().compile(optimize_pipeline, str(out))
    text = out.read_text()
    assert "agent_opt_module" in text
    assert "experiment_id" in text
    assert "agent_tag" in text


def test_optimize_pipeline_bakes_mcp_urls():
    # GEPA runs the agent locally, so the MCP Cloud Run URLs (not just the
    # registry names) must be in the wired runtime env.
    from src.pipelines import optimize_pipeline as op

    assert "SEARCH_MCP_URL" in op._RUNTIME_ENV
    assert "BOOKING_MCP_URL" in op._RUNTIME_ENV
    assert "EXPENSE_MCP_URL" in op._RUNTIME_ENV


def test_optimize_pipeline_enables_vertex_mode():
    # GEPA runs the agent's model client in-container, so google-genai must be
    # told to use Vertex (with project/location) — otherwise it defaults to the
    # Developer API, finds no key, and every inference fails → 0.0 scores.
    from src.pipelines import optimize_pipeline as op

    assert op._RUNTIME_ENV.get("GOOGLE_GENAI_USE_VERTEXAI") == "1"
    assert op._RUNTIME_ENV.get("GOOGLE_CLOUD_PROJECT")
    assert op._RUNTIME_ENV.get("GOOGLE_CLOUD_LOCATION")
