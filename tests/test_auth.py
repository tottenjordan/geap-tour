"""ADC token minting must survive WIF service-account impersonation.

This file exists because of a bug that was invisible everywhere except the one
place it mattered. `google.auth.default()` with no `scopes` is fine for a user
credential or an SA key — every developer machine — but under Workload Identity
Federation with impersonation the scope list is forwarded verbatim into the IAM
`generateAccessToken` body (`impersonated_credentials.py`: `"scope":
self._target_scopes`). Empty scopes means an empty `scope` field, and IAM answers
400 INVALID_ARGUMENT.

Result: the monitoring workflow's "Verify engine config" step reported every
engine UNREACHABLE on every scheduled run for weeks, while the client-library
steps beside it authenticated fine — they pass their own scopes. It went
unnoticed because `continue-on-error` rewrites the Actions API's `conclusion` to
"success"; only `outcome` showed it.

No network: `google.auth.default` is stubbed.
"""

from __future__ import annotations

import pytest

from src.auth import CLOUD_PLATFORM_SCOPE, adc_bearer_token


class _Creds:
    def __init__(self):
        self.token = None
        self.refreshed = False

    def refresh(self, _request):
        self.refreshed = True
        self.token = "ya29.fake"


@pytest.fixture
def captured(monkeypatch):
    """Stub google.auth.default and record the scopes it was called with."""
    seen: dict = {}
    creds = _Creds()

    def fake_default(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return creds, "proj"

    # google.auth.transport.requests is left real — it is import-safe without
    # credentials, and the stubbed creds ignore the Request it is handed.
    monkeypatch.setattr("google.auth.default", fake_default)
    seen["creds"] = creds
    return seen


class TestScopesAreAlwaysRequested:
    def test_the_default_call_requests_cloud_platform(self, captured):
        """THE regression. An unscoped default() 400s under WIF impersonation."""
        adc_bearer_token()
        assert captured["kwargs"]["scopes"] == [CLOUD_PLATFORM_SCOPE]

    def test_scopes_are_never_empty(self, captured):
        """Empty scopes is precisely what IAM rejects — not merely a narrower token."""
        adc_bearer_token()
        assert captured["kwargs"]["scopes"], "empty scopes -> 400 INVALID_ARGUMENT"

    def test_a_caller_may_narrow_them(self, captured):
        adc_bearer_token(scopes=["https://www.googleapis.com/auth/monitoring.read"])
        assert captured["kwargs"]["scopes"] == ["https://www.googleapis.com/auth/monitoring.read"]

    def test_an_empty_list_falls_back_rather_than_reinstating_the_bug(self, captured):
        """`scopes=[]` is the failure mode spelled out longhand; treat it as unset."""
        adc_bearer_token(scopes=[])
        assert captured["kwargs"]["scopes"] == [CLOUD_PLATFORM_SCOPE]

    def test_the_token_is_refreshed_before_it_is_returned(self, captured):
        token = adc_bearer_token()
        assert captured["creds"].refreshed is True
        assert token == "ya29.fake"


class TestBothCallersShareIt:
    """The two hand-rolled REST paths had identical code AND an identical bug. The
    point of the shared helper is that a fix cannot be half-applied."""

    def test_verify_engine_config_uses_the_shared_helper(self, captured):
        from src.deploy.verify_engine_config import _default_token

        assert _default_token() == "ya29.fake"
        assert captured["kwargs"]["scopes"] == [CLOUD_PLATFORM_SCOPE]

    def test_raw_stream_uses_the_shared_helper(self, captured):
        from src.eval.raw_stream import _default_token

        assert _default_token() == "ya29.fake"
        assert captured["kwargs"]["scopes"] == [CLOUD_PLATFORM_SCOPE]

    def test_no_module_still_calls_default_without_scopes(self):
        """A new hand-rolled copy would reintroduce this.

        `src/mcp_servers/**` is exempt — it runs on Cloud Run under a real service
        account, so there is no impersonation step to reject empty scopes.
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1] / "src"
        offenders = [
            f"{path.relative_to(root)}:{line}"
            for path in root.rglob("*.py")
            if "mcp_servers" not in path.parts
            for line in unscoped_adc_calls(path.read_text())
        ]
        assert not offenders, f"unscoped google.auth.default() will 400 under WIF: {offenders}"


def unscoped_adc_calls(source: str) -> list[int]:
    """Line numbers of `google.auth.default()` calls with no `scopes=`.

    Parsed with `ast`, not grepped, for two reasons. A regex matches the prose in
    this file and in `src/auth.py` that *explains* the bug, so a grep-based guard
    fails on its own documentation. And a regex anchored on `google.auth.default`
    misses the `from google.auth import default` form entirely — which is the same
    bug, spelled differently, and would sail past the guard.

    Handles all three spellings: the attribute form, the from-import, and an
    aliased from-import. `TestTheGuardItself` exercises each, because a scanner
    that silently matches nothing passes the repo-wide check vacuously — which is
    exactly how the first version of this guard shipped broken.
    """
    import ast

    def dotted(node) -> str:
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        return ".".join(reversed(parts))

    tree = ast.parse(source)

    # Names bound locally to google.auth.default via `from ... import default`.
    local_aliases = {
        (alias.asname or alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("google.auth")
        for alias in node.names
        if alias.name == "default"
    }

    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or any(kw.arg == "scopes" for kw in node.keywords):
            continue
        name = dotted(node.func)
        if name.endswith("auth.default") or (
            isinstance(node.func, ast.Name) and name in local_aliases
        ):
            out.append(node.lineno)
    return out


class TestTheGuardItself:
    """The scanner must be shown to catch things, not merely to pass.

    Its first version matched only the attribute form, so
    `from google.auth import default; default()` — the identical bug — would have
    reintroduced the outage and left the suite green.
    """

    def test_it_catches_the_attribute_form(self):
        assert unscoped_adc_calls("import google.auth\nx = google.auth.default()") == [2]

    def test_it_catches_the_from_import_form(self):
        assert unscoped_adc_calls("from google.auth import default\nx = default()") == [2]

    def test_it_catches_an_aliased_from_import(self):
        assert unscoped_adc_calls("from google.auth import default as gad\nx = gad()") == [2]

    def test_it_ignores_a_correctly_scoped_call(self):
        assert unscoped_adc_calls('import google.auth\nx = google.auth.default(scopes=["s"])') == []

    def test_it_ignores_prose_about_the_bug(self):
        """This file and src/auth.py both describe `google.auth.default()` in
        docstrings; a grep-based guard would fail on its own explanation."""
        assert unscoped_adc_calls('"""we used to call google.auth.default() here"""') == []

    def test_an_unrelated_default_is_not_flagged(self):
        """`default()` only counts when it was imported FROM google.auth."""
        assert unscoped_adc_calls("from collections import default\nx = default()") == []
