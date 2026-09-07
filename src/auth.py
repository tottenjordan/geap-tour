"""One place to mint an ADC bearer token for hand-rolled REST calls.

Most of this repo talks to Google Cloud through client libraries, which handle
auth themselves. Two paths do not — the control-plane GET in
:mod:`src.deploy.verify_engine_config` and the raw-SSE reader in
:mod:`src.eval.raw_stream` — and both hand-rolled the same three lines.

**They also both hand-rolled the same bug.** ``google.auth.default()`` with no
``scopes`` is fine for a user credential or a service-account key, so it works on
every developer machine. Under **Workload Identity Federation with service-account
impersonation** — how CI authenticates — ``default()`` returns credentials whose
refresh calls IAM ``generateAccessToken``, and the scope list goes straight into
that request body (``google/auth/impersonated_credentials.py``: ``"scope":
self._target_scopes``). Empty scopes means an empty ``scope`` field, and IAM
answers **400 INVALID_ARGUMENT**, surfaced as::

    ('Unable to acquire impersonated credentials',
     '{"error": {"code": 400, "message": "Request contains an invalid argument."}}')

That is not a hypothetical: the monitoring workflow's "Verify engine config" step
failed this way on **every scheduled run for weeks**, reporting the engine as
``UNREACHABLE`` while the client-library steps beside it authenticated fine. It
went unnoticed because the step is ``continue-on-error``, which rewrites the
Actions API's ``conclusion`` to ``success`` — only ``outcome`` showed the failure.

So: one helper, explicit scopes, imported by both callers, so the two cannot
drift apart again and the fix cannot be half-applied.
"""

from __future__ import annotations

# The scope client libraries request by default. Impersonation requires a
# non-empty scope list; this is the one that covers every API we call.
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def adc_bearer_token(scopes: list[str] | None = None) -> str:
    """Refreshed ADC access token, scoped so WIF impersonation can mint it.

    Pass ``scopes`` only to narrow it; the default is what the client libraries
    use and what the impersonation path needs.
    """
    import google.auth
    import google.auth.transport.requests as gart

    creds, _ = google.auth.default(scopes=scopes or [CLOUD_PLATFORM_SCOPE])
    creds.refresh(gart.Request())
    return creds.token
