"""The serving configuration a deployed Agent Engine must have, and why.

Every setting here was paid for by an outage, a silent degradation, or a
measured experiment — the rationale is in each check's ``why``, and the note
[docs/notes/deployed-engine-baseline.md](../../docs/notes/deployed-engine-baseline.md)
collects the evidence. This module is the **executable** copy of that note, so
the two cannot drift the way a prose runbook does.

Two properties make it worth having as code rather than a checklist:

1. **One source of truth with the deployer.** The memory/cpu expectations are
   imported from :mod:`src.deploy.deploy_agents`, so "what we deploy" and "what
   we verify" are the same constants by construction.
2. **Pure.** :func:`evaluate` takes a normalized spec dict and returns findings.
   It touches no network, so the whole rule set is unit-testable and
   :mod:`src.deploy.verify_engine_config` only has to supply the fetch.

The checks are deliberately *not* a schema dump of everything a deploy sets. A
setting earns a check by having a failure mode we have actually hit: a silent
one (wrong tier models, no telemetry, registry fallback) or an expensive one
(4Gi OOM). Settings that are merely present — model ids, MCP URLs, thresholds —
are left to the deploy path.

**Severity has a precise meaning here:**

* ``critical`` — the engine is serving wrong or dropping traffic. Fails CI.
* ``advisory`` — a posture or cost/latency choice worth seeing. Never fails.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from src.armor.config import server_side_armor_enabled
from src.deploy.deploy_agents import LITELLM_CPU, LITELLM_MEMORY

if TYPE_CHECKING:
    from collections.abc import Callable

Severity = Literal["critical", "advisory"]

# Roles a deployed engine can play. The router's tier wiring and the
# coordinator's memory wiring have no overlap, so they get separate rule sets on
# top of the shared ones.
ROLES = ("coordinator", "router")

# Keep-warm floor. The value is a judgement, not a measurement — see the check's
# `why`. 0/unset means scale-to-zero, which is the only value we know is wrong.
MIN_INSTANCES_FLOOR = 1

# A "thinking" model returns its budget as reasoning and leaves the text empty,
# which makes classify_complexity fall back to a low score for EVERY prompt (so
# the router sends all traffic to the lite tier and the 5-tier demo is a lie).
NON_THINKING_CLASSIFIERS = ("gemini-2.5-flash-lite", "gemini-2.0-flash-lite")


@dataclass(frozen=True)
class Finding:
    """One evaluated check against one engine."""

    name: str
    ok: bool
    severity: Severity
    expected: str
    observed: str
    why: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "severity": self.severity,
            "expected": self.expected,
            "observed": self.observed,
            "why": self.why,
        }


@dataclass(frozen=True)
class Check:
    name: str
    severity: Severity
    expected: str
    why: str
    predicate: Callable[[dict], tuple[bool, str]]
    """``spec -> (ok, observed)``. Observed is rendered verbatim in the report."""


def _env(spec: dict, key: str) -> str:
    return (spec.get("env") or {}).get(key) or ""


def _limits(spec: dict) -> dict:
    return spec.get("resource_limits") or {}


# --------------------------------------------------------------------- shared

SHARED_CHECKS: tuple[Check, ...] = (
    Check(
        name="memory",
        severity="critical",
        expected=LITELLM_MEMORY,
        why=(
            "The Agent Runtime default of 4Gi OOM-kills workers mid-call on EVERY "
            "backbone, not just LiteLlm ones — the client sees HTTP 200 with zero "
            "characters and there is no traceback and no shutdown log, because a "
            "SIGKILL emits neither. Measured on one Gemini-only coordinator, same "
            "cases: 22/147 empty (180 empty attempts) at 4Gi, 0/147 (0 attempts) "
            "at 16Gi. See docs/notes/empty-at-200-field-guide.md cause 5."
        ),
        predicate=lambda s: (
            _limits(s).get("memory") == LITELLM_MEMORY,
            _limits(s).get("memory") or "(platform default 4Gi)",
        ),
    ),
    Check(
        name="cpu",
        severity="critical",
        expected=LITELLM_CPU,
        why="Paired with the memory limit; resource_limits must set both or neither.",
        predicate=lambda s: (
            _limits(s).get("cpu") == LITELLM_CPU,
            _limits(s).get("cpu") or "(platform default)",
        ),
    ),
    Check(
        name="identity",
        severity="critical",
        expected="AGENT_IDENTITY",
        why=(
            "A per-engine SPIFFE identity is what the Agent Registry grant is made "
            "to. Without it the engine authenticates as a shared service account "
            "and `roles/agentregistry.viewer` on the engine principal buys nothing."
        ),
        predicate=lambda s: (
            s.get("identity_type") == "AGENT_IDENTITY",
            str(s.get("identity_type") or "(default)"),
        ),
    ),
    Check(
        name="telemetry",
        severity="critical",
        expected="GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true",
        why=(
            "Without it the engine emits no span tree, so Cloud Trace and the "
            "console Observability tab are blank and every trace-derived "
            "diagnosis in docs/notes becomes impossible to reproduce."
        ),
        predicate=lambda s: (
            _env(s, "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY") == "true",
            _env(s, "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY") or "(unset)",
        ),
    ),
    Check(
        name="vertex_backend",
        severity="critical",
        expected="GOOGLE_GENAI_USE_VERTEXAI=1",
        why=(
            "google-genai otherwise takes the Gemini Developer API path, which "
            "needs an API key the engine does not have."
        ),
        predicate=lambda s: (
            _env(s, "GOOGLE_GENAI_USE_VERTEXAI") == "1",
            _env(s, "GOOGLE_GENAI_USE_VERTEXAI") or "(unset)",
        ),
    ),
    Check(
        name="mcp_registry_names",
        severity="critical",
        expected="SEARCH/BOOKING/EXPENSE_MCP_SERVER all set",
        why=(
            "These are the Agent Registry resource names for the primary "
            "resolution path. Missing ones crash the agent on import; the "
            "direct-URL fallback only covers a registry *failure*, not absence."
        ),
        predicate=lambda s: _all_set(
            s, ("SEARCH_MCP_SERVER", "BOOKING_MCP_SERVER", "EXPENSE_MCP_SERVER")
        ),
    ),
    Check(
        name="genai_enterprise_alias",
        severity="advisory",
        expected="GOOGLE_GENAI_USE_ENTERPRISE=1",
        why=(
            "ADK 2.7.1 / google-genai 2.19 read this first and only fall back to "
            "GOOGLE_GENAI_USE_VERTEXAI with a DeprecationWarning. Behaviour is "
            "identical today, so this is log hygiene, not correctness."
        ),
        predicate=lambda s: (
            _env(s, "GOOGLE_GENAI_USE_ENTERPRISE") == "1",
            _env(s, "GOOGLE_GENAI_USE_ENTERPRISE") or "(unset)",
        ),
    ),
    Check(
        name="min_instances",
        severity="advisory",
        expected=f">= {MIN_INSTANCES_FLOOR}",
        why=(
            "Scale-to-zero means the first request after idle pays a cold start, "
            "which on this platform surfaces as a slow or error-shaped stream "
            "rather than a queued request. Our engines run 4. Honest caveat: the "
            "one measurement attributing empties to min_instances=1 was taken "
            "BEFORE the 4Gi OOM was found and is confounded by it — so treat this "
            "as a latency/demo-readiness floor, not a proven empty-stream fix."
        ),
        predicate=lambda s: (
            (s.get("min_instances") or 0) >= MIN_INSTANCES_FLOOR,
            str(s.get("min_instances") or "(scale to zero)"),
        ),
    ),
    Check(
        name="own_engine_id",
        severity="advisory",
        expected="AGENT_ENGINE_ID == this engine",
        why=(
            "Sessions and Memory Bank are safe regardless — _runtime_engine_id() "
            "prefers the runtime-injected GOOGLE_CLOUD_AGENT_ENGINE_ID. But a "
            "baked AGENT_ENGINE_ID naming a *different* engine still feeds "
            "config-derived client values (e.g. coordinator_a2a_url) and makes "
            "logs read as though the wrong engine is serving."
        ),
        predicate=lambda s: (
            _env(s, "AGENT_ENGINE_ID") == s.get("engine_id"),
            _env(s, "AGENT_ENGINE_ID") or "(unset)",
        ),
    ),
)


def _all_set(spec: dict, keys: tuple[str, ...]) -> tuple[bool, str]:
    missing = [k for k in keys if not _env(spec, k)]
    return (not missing, "all set" if not missing else f"missing {', '.join(missing)}")


# ---------------------------------------------------------------- coordinator

COORDINATOR_CHECKS: tuple[Check, ...] = (
    Check(
        name="memory_bank",
        severity="critical",
        expected="ENABLE_MEMORY_BANK=1",
        why=(
            "Cross-session recall is the coordinator's headline capability. With "
            "this off the agent still answers, so the loss is silent until a demo "
            "asks it to remember something."
        ),
        predicate=lambda s: (
            _env(s, "ENABLE_MEMORY_BANK") == "1",
            _env(s, "ENABLE_MEMORY_BANK") or "(unset)",
        ),
    ),
    Check(
        name="memory_preload_cache",
        severity="advisory",
        expected="ENABLE_MEMORY_PRELOAD_CACHE=1",
        why=(
            "ADK's PreloadMemoryTool re-runs a blocking 3-5s Memory Bank retrieve "
            "before EVERY internal LLM hop with the same query. The caching "
            "subclass collapses that to once per invocation, with no "
            "cross-invocation staleness (a new invocation always misses). It also "
            "emits the only span that shows whether the collapse happened."
        ),
        predicate=lambda s: (
            _env(s, "ENABLE_MEMORY_PRELOAD_CACHE") == "1",
            _env(s, "ENABLE_MEMORY_PRELOAD_CACHE") or "(unset — stock tool)",
        ),
    ),
    Check(
        name="server_side_armor",
        severity="advisory",
        expected="COORDINATOR_MODEL on the regional-Gemini path",
        why=(
            "Model Armor templates are region-scoped and only honored for a "
            "Gemini-2.x backbone. On Gemini-3 (global endpoint) they 400 and on "
            "Claude (LiteLlm) they are never sent, so get_armored_generate_config "
            "omits them and the client-side guardrail is the ONLY screening layer. "
            "That is a supported posture, not a bug — but it should be a decision, "
            "and the baked MODEL_ARMOR_* env makes it look active when it is not."
        ),
        predicate=lambda s: (
            server_side_armor_enabled(_env(s, "COORDINATOR_MODEL")),
            f"{_env(s, 'COORDINATOR_MODEL') or '(unset)'} -> "
            + (
                "templates active"
                if server_side_armor_enabled(_env(s, "COORDINATOR_MODEL"))
                else "client-side guardrail only"
            ),
        ),
    ),
)


# --------------------------------------------------------------------- router


def _tier_models_are_regional(spec: dict) -> tuple[bool, str]:
    """The router's Gemini tiers must be pinned to 2.x.

    This is the repo's single nastiest deploy trap: a plain
    ``deploy_agents router --update`` bakes ``src/config``'s Gemini-3 defaults,
    and the router's Gemini tiers regress. The tier env overrides are mandatory
    on every router deploy, and nothing else enforces that.
    """
    tiers = {k: _env(spec, k) for k in ("LITE_MODEL", "FLASH_MODEL", "PRO_MODEL")}
    regressed = {k: v for k, v in tiers.items() if v.startswith("gemini-3")}
    observed = ", ".join(f"{k.split('_')[0].lower()}={v or '(unset)'}" for k, v in tiers.items())
    return (not regressed, observed)


ROUTER_CHECKS: tuple[Check, ...] = (
    Check(
        name="tier_models_pinned",
        severity="critical",
        expected="LITE/FLASH/PRO_MODEL not gemini-3*",
        why=(
            "A plain `deploy_agents router --update` bakes config.py's Gemini-3 "
            "defaults and silently regresses the tiers — the deploy succeeds and "
            "the engine serves, just on models the router was not tuned for. The "
            "tier env overrides are mandatory on every router deploy."
        ),
        predicate=_tier_models_are_regional,
    ),
    Check(
        name="classifier_non_thinking",
        severity="critical",
        expected=f"CLASSIFIER_MODEL in {NON_THINKING_CLASSIFIERS}",
        why=(
            "A thinking classifier spends its budget on reasoning and returns "
            "empty text, so classify_complexity takes its low-score fallback for "
            "every prompt and the router sends ALL traffic to the lite tier. It "
            "still answers, so the collapse is invisible without checking routes."
        ),
        predicate=lambda s: (
            _env(s, "CLASSIFIER_MODEL") in NON_THINKING_CLASSIFIERS,
            _env(s, "CLASSIFIER_MODEL") or "(unset)",
        ),
    ),
)


def checks_for(role: str) -> tuple[Check, ...]:
    """Shared checks plus the role's own. Unknown roles get the shared set."""
    extra = {"coordinator": COORDINATOR_CHECKS, "router": ROUTER_CHECKS}.get(role, ())
    return SHARED_CHECKS + extra


def infer_role(spec: dict) -> str:
    """Best-effort role from the display name (``--role`` overrides).

    Deliberately conservative: an unrecognised name gets the shared checks only,
    which is right for the leaf agents (travel/expense/tiers) too.
    """
    name = (spec.get("display_name") or "").lower()
    for role in ROLES:
        if role in name:
            return role
    return "unknown"


def evaluate(spec: dict, role: str | None = None) -> list[Finding]:
    """Run the rule set for ``role`` against a normalized engine ``spec``.

    ``spec`` keys: ``engine_id``, ``display_name``, ``identity_type``,
    ``min_instances``, ``resource_limits`` (``{cpu, memory}``), ``env``.
    """
    resolved = role or infer_role(spec)
    findings = []
    for check in checks_for(resolved):
        ok, observed = check.predicate(spec)
        findings.append(
            Finding(
                name=check.name,
                ok=ok,
                severity=check.severity,
                expected=check.expected,
                observed=observed,
                why=check.why,
            )
        )
    return findings


def has_critical_drift(findings: list[Finding]) -> bool:
    return any(not f.ok and f.severity == "critical" for f in findings)
