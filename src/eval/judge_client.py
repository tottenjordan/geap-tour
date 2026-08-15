"""Shared deterministic + retrying judge client for every LLM-judge scorer.

Each standalone judge (``policy_judge``, ``tool_use_judge``, ``online_monitor``,
``pairwise_eval``) previously called the judge model through an identical
``_default_generate_fn`` that ran a bare
``client.models.generate_content(model=..., contents=prompt)`` — **no temperature
pinned** (so scores drift run-to-run) and **no retry** (so a transient empty or
errored verdict was silently dropped from the mean, shrinking the sample). This
module centralizes one judge call that all four reuse:

* ``temperature=0`` by default → reproducible verdicts.
* bounded retry with linear backoff on empty text *or* a raised transport error.

The retry core (:func:`generate_with_retry`) is pure and unit-tested with an
injected ``sleep``; :func:`build_judge_generate_fn` wires it to a Vertex-backend
``google.genai`` client (also injectable for tests). On exhaustion it returns
``""`` — the callers already treat empty judge text as "unparseable → drop from
the mean", so behavior degrades exactly as before, only after retrying first.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)

DEFAULT_TEMPERATURE = 0.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_S = 2.0


def generate_with_retry(
    call: Callable[[], object],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_s: float = DEFAULT_BACKOFF_S,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Run a judge ``call`` (returns an object with ``.text``); retry on empty/error.

    Retries on both a raised exception and empty/whitespace text, sleeping
    ``backoff_s * attempt`` (linear) between tries. Returns the stripped judge
    text, or ``""`` once ``max_attempts`` are exhausted (the callers drop empty
    verdicts from the average). ``sleep`` is injectable so tests incur no delay.
    """
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = call()
            text = (getattr(resp, "text", None) or "").strip()
            if text:
                return text
            last_err = None
        except Exception as err:  # judge transport is best-effort; retry then drop
            last_err = err
        if attempt < max_attempts:
            sleep(backoff_s * attempt)
    if last_err is not None:
        log.warning("judge call failed after %d attempts: %s", max_attempts, last_err)
    else:
        log.warning("judge returned empty text after %d attempts", max_attempts)
    return ""


def build_judge_generate_fn(
    judge_model: str,
    project: str | None = None,
    location: str | None = None,
    *,
    temperature: float = DEFAULT_TEMPERATURE,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_s: float = DEFAULT_BACKOFF_S,
    sleep: Callable[[float], None] = time.sleep,
    client=None,
) -> Callable[[str], str]:
    """Return a ``prompt -> judge_text`` fn: deterministic (temperature) + retrying.

    Centralizes the previously-duplicated ``_default_generate_fn`` across the
    policy, tool-use, online-monitor, and pairwise judges. ``client`` is
    injectable for tests; otherwise a Vertex-backend ``google.genai`` client is
    built from the project/location (defaulting to the repo config).
    """
    from google.genai import types

    if client is None:
        from google import genai

        from src.config import GCP_PROJECT_ID, GCP_REGION

        client = genai.Client(
            vertexai=True,
            project=project or GCP_PROJECT_ID,
            location=location or GCP_REGION,
        )

    config = types.GenerateContentConfig(temperature=temperature)

    def _generate(prompt: str) -> str:
        return generate_with_retry(
            lambda: client.models.generate_content(
                model=judge_model, contents=prompt, config=config
            ),
            max_attempts=max_attempts,
            backoff_s=backoff_s,
            sleep=sleep,
        )

    return _generate
