"""The retro-label script builds correct PATCH requests (pure, no network)."""

from scripts.apply_resource_labels import _armor_patch, _engine_patch


def test_engine_patch_request_shape():
    url, body = _engine_patch(
        "projects/p/locations/us-central1/reasoningEngines/999",
        {"solution": "geap-tour"},
    )
    assert url.endswith("reasoningEngines/999?updateMask=labels")
    assert url.startswith("https://us-central1-aiplatform.googleapis.com/v1/")
    assert body == {"labels": {"solution": "geap-tour"}}


def test_armor_patch_request_shape():
    url, body = _armor_patch(
        "projects/p/locations/us-central1/templates/geap-workshop-prompt",
        {"solution": "geap-tour"},
    )
    assert url.endswith("templates/geap-workshop-prompt?updateMask=labels")
    assert body == {"labels": {"solution": "geap-tour"}}
