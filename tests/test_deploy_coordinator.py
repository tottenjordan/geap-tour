"""Offline tests for the bake-off's single-engine deploy entrypoint.

``src.doe.deploy_coordinator`` is a tiny CLI run in its OWN interpreter (one per
backbone) so ``COORDINATOR_MODEL`` bakes at import time inside a fresh process.
It deploys one persistent coordinator engine and prints its resource name on a
marker line the bake-off orchestrator captures from stdout. Both the deploy call
and the agent are injectable so these tests assert the wiring (marker emitted,
resource returned) without touching GCP.
"""

from src.doe import deploy_coordinator as dc


def test_main_prints_resource_marker(capsys):
    resource = "projects/p/locations/global/reasoningEngines/123"
    calls = {}

    def fake_deploy(agent, display_name=None):
        calls["agent"] = agent
        calls["display_name"] = display_name
        return resource

    rc = dc.main(
        ["--display-name", "coordinator_agent_bakeoff_gemini"],
        deploy_fn=fake_deploy,
        agent=object(),
    )

    assert rc == 0
    out = capsys.readouterr().out
    # The resource name is emitted on a marker line, as the LAST such line.
    marker_lines = [ln for ln in out.splitlines() if ln.startswith(dc.RESOURCE_MARKER)]
    assert marker_lines, f"no marker line in output:\n{out}"
    assert marker_lines[-1] == f"{dc.RESOURCE_MARKER}{resource}"
    # The display name flowed through to the deploy call.
    assert calls["display_name"] == "coordinator_agent_bakeoff_gemini"


def test_parse_resource_from_output_reads_last_marker():
    # Even with chatty deploy logs before/after, the marker line is recoverable.
    stdout = (
        "--- Creating coordinator_agent ---\n"
        "  Identity: default\n"
        f"{dc.RESOURCE_MARKER}projects/p/locations/global/reasoningEngines/999\n"
        "trailing noise\n"
    )
    assert (
        dc.parse_resource_from_output(stdout) == "projects/p/locations/global/reasoningEngines/999"
    )


def test_parse_resource_returns_none_when_absent():
    assert dc.parse_resource_from_output("no marker here\njust logs\n") is None
