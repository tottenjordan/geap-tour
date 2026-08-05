"""Full-parity KFP v2 eval pipeline: deploy -> traffic -> (batch || simulated
|| complexity) -> monitor-verify -> report, with guaranteed temp-engine cleanup.

Mirrors ``src/eval/run_all_evals.py`` but runs the DAG on Vertex AI Managed
Pipelines instead of a GitHub Actions job graph.

Deployment-specific config (MCP Agent Registry names + router engine id) is read
from ``src.config`` at import time and baked onto every task as *static* env
vars via ``set_env_variable``. This is deliberate: KFP rejects passing a
pipeline *parameter* to ``set_env_variable`` (it needs a plain string), and
``src.config`` / ``src.registry`` read these vars at import time inside each
component. ``submit.py`` recompiles on every run, so the baked values always
reflect the current ``.env``.
"""

from kfp import dsl

from src.config import (
    BOOKING_MCP_SERVER,
    EXPENSE_MCP_SERVER,
    ROUTER_ENGINE_ID,
    SEARCH_MCP_SERVER,
)
from src.pipelines import components as c

# Deployment env applied to EVERY task before any ``src.*`` import runs.
_RUNTIME_ENV = {
    "SEARCH_MCP_SERVER": SEARCH_MCP_SERVER,
    "BOOKING_MCP_SERVER": BOOKING_MCP_SERVER,
    "EXPENSE_MCP_SERVER": EXPENSE_MCP_SERVER,
    "ROUTER_ENGINE_ID": ROUTER_ENGINE_ID,
}


def _wire(task):
    """Bake deployment env onto a task as static env vars."""
    for key, value in _RUNTIME_ENV.items():
        task.set_env_variable(key, value)
    return task


@dsl.pipeline(
    name="geap-eval-pipeline",
    pipeline_root="gs://geap-tour-staging-v2/pipeline-root",
)
def eval_pipeline(
    agent_id: str = "",
    agent_module: str = "coordinator_agent",
    threshold: float = 3.0,
    skip_traffic: bool = False,
    traffic_count: int = 2,
    scenario_count: int = 5,
    max_turns: int = 3,
    temp_display_name: str = "",
):
    # cleanup is the exit task: it may only reference pipeline params (KFP forbids
    # exit tasks from depending on other tasks), so it takes agent_id +
    # temp_display_name and finds/deletes the fresh engine by display_name.
    with dsl.ExitHandler(
        _wire(c.cleanup(agent_id=agent_id, display_name=temp_display_name))
    ):
        resolve = _wire(
            c.resolve_agent(
                agent_id=agent_id,
                agent_module=agent_module,
                display_name=temp_display_name,
            )
        )
        agent_res = resolve.outputs["agent_resource"]

        with dsl.If(skip_traffic == False):  # noqa: E712 - KFP needs the explicit compare
            _wire(c.generate_traffic(agent_resource=agent_res, count=traffic_count))

        batch = _wire(c.batch_eval(agent_resource=agent_res, threshold=threshold))
        sim = _wire(
            c.simulated_eval(
                agent_resource=agent_res,
                threshold=threshold,
                scenario_count=scenario_count,
                max_turns=max_turns,
            )
        )
        comp = _wire(c.complexity_eval())
        # Preserve run_all_evals' monitor-after-batch ordering; batch/sim/comp
        # otherwise run in parallel.
        mon = _wire(c.monitor_verify(agent_resource=agent_res)).after(batch)

        _wire(
            c.report(
                batch_results=batch.outputs["results"],
                sim_results=sim.outputs["results"],
                complexity_results=comp.outputs["results"],
                monitor_results=mon.outputs["results"],
            )
        )
