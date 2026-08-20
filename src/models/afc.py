"""Explicitly disable google-genai automatic function calling (AFC).

``google.genai._extra_utils.should_disable_afc`` defaults AFC **on** for any
config that does not say otherwise, so every ``generate_content`` /
``generate_content_stream`` call takes the AFC branch. That branch logs
``INFO: AFC is enabled with max remote calls: N`` on **every call**, plus a
WARNING recommending ``AsyncChat.send_message`` once per class per process. On
the deployed router that was 1000+ warning rows in six hours — the managed
runtime spreads requests across many worker processes and each one logs it once.

Disabling AFC is behaviour-preserving here: ADK runs its own function-calling
loop and passes tool *declarations* (``types.Tool``), never Python callables, so
the AFC loop's function map is always empty — it makes one call and breaks.
Turning it off skips a per-call ``model_copy(deep=True)`` and, when streaming, an
extra output accumulation. It does **not** disable ADK tool calling.

See docs/notes/genai-afc-warning.md.
"""

from google.genai.types import AutomaticFunctionCallingConfig, GenerateContentConfig


def with_afc_disabled(config: GenerateContentConfig | None = None) -> GenerateContentConfig:
    """Return ``config`` (or a fresh config) with AFC explicitly disabled.

    Returns a copy — the caller's config is never mutated — carrying a fresh
    ``AutomaticFunctionCallingConfig``, so no two configs share one mutable
    pydantic object.
    """
    base = config if config is not None else GenerateContentConfig()
    return base.model_copy(
        update={"automatic_function_calling": AutomaticFunctionCallingConfig(disable=True)}
    )
