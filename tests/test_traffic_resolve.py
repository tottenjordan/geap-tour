"""Engine-id resolution for the traffic generator's entry points.

The traffic CLI/callers advertise "resource name or engine ID", but
``agent_engines.get`` needs the full ``projects/.../reasoningEngines/<id>`` name.
Only the ``None`` (auto-detect) case used to build a full name, so a supplied
**bare** id 404'd. These tests pin the shared resolver and prove the burst path
resolves before calling ``agent_engines.get``. No GCP is touched.
"""

import pytest

from src.traffic import generate_traffic as gt


class TestResolveEngineResource:
    def test_none_falls_back_to_default_engine_id(self):
        resolved = gt._resolve_engine_resource(None, "DEFAULT_ID")
        assert resolved.endswith("/reasoningEngines/DEFAULT_ID")
        assert resolved.startswith("projects/")

    def test_bare_id_resolves_to_full_resource_name(self):
        resolved = gt._resolve_engine_resource("12345", "DEFAULT_ID")
        assert resolved.endswith("/reasoningEngines/12345")
        assert resolved.startswith("projects/")
        assert "DEFAULT_ID" not in resolved  # supplied id wins over the default

    def test_full_resource_name_passed_through_unchanged(self):
        full = "projects/p/locations/us-central1/reasoningEngines/abc"
        assert gt._resolve_engine_resource(full, "DEFAULT_ID") == full


class TestGenerateTrafficResolvesBeforeGet:
    def test_bare_id_is_resolved_before_agent_engines_get(self, monkeypatch):
        seen = {}

        def _fake_get(name):
            seen["name"] = name
            raise RuntimeError("stop after resolution")  # short-circuit the live path

        monkeypatch.setattr(gt.vertexai, "init", lambda **_: None)
        monkeypatch.setattr(gt, "disable_pyopenssl", lambda: None)
        monkeypatch.setattr(gt.agent_engines, "get", _fake_get)

        with pytest.raises(RuntimeError, match="stop after resolution"):
            gt.generate_traffic("12345")

        assert seen["name"].endswith("/reasoningEngines/12345")
        assert seen["name"].startswith("projects/")
