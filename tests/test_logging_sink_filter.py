"""Guard: the BQ log sink must filter on the resource type engines actually log
under. Deployed Agent Engines emit logs as `ReasoningEngine`, not `AgentEngine`
(the old value silently matched nothing, leaving geap_workshop_logs empty)."""

import pathlib

SCRIPT = pathlib.Path("scripts/setup_logging_sink.sh")


def test_sink_filter_uses_reasoning_engine():
    text = SCRIPT.read_text()
    assert 'resource.type="aiplatform.googleapis.com/ReasoningEngine"' in text


def test_sink_filter_does_not_use_stale_agent_engine_type():
    text = SCRIPT.read_text()
    assert 'aiplatform.googleapis.com/AgentEngine' not in text
