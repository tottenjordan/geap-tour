"""``with_afc_disabled`` stamps AFC off on a GenerateContentConfig.

google-genai defaults automatic function calling **on** for any config that does
not explicitly disable it, which makes every ``generate_content`` call take the
AFC branch and log a per-call INFO plus a once-per-process WARNING.

These tests assert on config *contents*, never on log output: the warning string
only exists in google-genai >= 2.18.1 and the dev venv is on 2.17.0, so a
log-based assertion would pass locally for the wrong reason.
"""

from google.genai.types import GenerateContentConfig

from src.models.afc import with_afc_disabled


def test_stamps_a_bare_config():
    cfg = with_afc_disabled()

    assert cfg.automatic_function_calling.disable is True


def test_preserves_every_other_field():
    base = GenerateContentConfig(temperature=0.0, max_output_tokens=2048)

    cfg = with_afc_disabled(base)

    assert cfg.temperature == 0.0
    assert cfg.max_output_tokens == 2048
    assert cfg.automatic_function_calling.disable is True


def test_does_not_mutate_the_callers_config():
    base = GenerateContentConfig(temperature=0.0)

    with_afc_disabled(base)

    assert base.automatic_function_calling is None


def test_each_call_gets_its_own_afc_object():
    """No shared mutable pydantic singleton across configs."""
    first = with_afc_disabled()
    second = with_afc_disabled()

    assert first.automatic_function_calling is not second.automatic_function_calling
