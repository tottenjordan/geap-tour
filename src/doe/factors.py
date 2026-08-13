"""Declarative registry of DOE factors and their levels.

Each factor varies one aspect of the agent/eval configuration. A factor has a
*channel* that determines how its level value reaches a pipeline run:

  - ``engine_env``  — env var baked into the *deployed engine* (model, prompt).
                      Changing it requires a fresh Agent Engine deploy, because
                      the engine reads config at import time inside its
                      container. These are the expensive factors.
  - ``runner_env``  — env var baked onto the in-pipeline tasks only (router
                      complexity boundaries affect the in-runner complexity_eval
                      via src.config → src.router.complexity). No engine deploy.
  - ``param``       — a pipeline *parameter* (eval fidelity: scenario_count,
                      max_turns, ...). Passed via parameter_values, no env at all.

Each level maps a human label to a concrete assignment:
  - env channels: ``{ENV_VAR: "string value"}`` (values must be strings).
  - param channel: ``{param_name: value}`` (native types; forwarded as CLI args).

Levels are ordered: the first is the coded ``-1`` (low) level, the second the
coded ``+1`` (high) level, matching the design generator's convention.
"""

from __future__ import annotations

from dataclasses import dataclass

CHANNELS = ("engine_env", "runner_env", "param")


@dataclass(frozen=True)
class Factor:
    """One experimental factor with exactly two ordered levels."""

    name: str
    channel: str
    levels: dict[str, dict]  # ordered: {low_label: {...}, high_label: {...}}
    description: str = ""

    def __post_init__(self) -> None:
        if self.channel not in CHANNELS:
            raise ValueError(f"{self.name}: channel {self.channel!r} not in {CHANNELS}")
        if len(self.levels) != 2:
            raise ValueError(
                f"{self.name}: expected exactly 2 levels, got {list(self.levels)}"
            )
        for label, mapping in self.levels.items():
            if not isinstance(mapping, dict) or not mapping:
                raise ValueError(f"{self.name}:{label}: level must be a non-empty dict")
            if self.channel in ("engine_env", "runner_env"):
                bad = [k for k, v in mapping.items() if not isinstance(v, str)]
                if bad:
                    raise ValueError(
                        f"{self.name}:{label}: env values must be strings ({bad})"
                    )

    @property
    def labels(self) -> list[str]:
        """Level labels in coded order: [low (-1), high (+1)]."""
        return list(self.levels)

    @property
    def low_label(self) -> str:
        return self.labels[0]

    @property
    def high_label(self) -> str:
        return self.labels[1]

    def assignment(self, label: str) -> dict:
        """Concrete env/param assignment for a level label."""
        return dict(self.levels[label])


# --- The four seed factors --------------------------------------------------

FACTORS: list[Factor] = [
    Factor(
        name="router_boundaries",
        channel="runner_env",
        description="Complexity cut-points; 'aggressive_savings' pushes traffic "
        "to cheaper tiers. NB: after screening doe-screening-20260812-073603 the "
        "'aggressive_savings' values became the src/config.py default, so this "
        "factor's 'baseline' level now contrasts the OLD default against the "
        "current one (a meaningful 'should we revert?' probe), not default-vs-new.",
        levels={
            "baseline": {
                "COMPLEXITY_LOW": "0.30",
                "MEDIUM_SPLIT": "0.45",
                "COMPLEXITY_HIGH": "0.60",
                "HIGH_SPLIT": "0.80",
            },
            "aggressive_savings": {
                # Placed in the classifier's score-cluster gaps (observed temp=0
                # scores: 0.10/0.15/0.20/0.45/0.75/0.85/0.90) so no cut-point
                # coincides with an emitted score — with the router's strict `<`,
                # a boundary sitting exactly on a score fails to reclassify it.
                # This set moves 7/12 router eval cases, exercises all five tiers
                # and eliminates opus. See docs/notes/doe-router-boundaries-inert.md.
                "COMPLEXITY_LOW": "0.44",
                "MEDIUM_SPLIT": "0.60",
                "COMPLEXITY_HIGH": "0.80",
                "HIGH_SPLIT": "0.95",
            },
        },
    ),
    Factor(
        name="model_tier",
        channel="engine_env",
        description="Sub-agent model: flash (baseline) vs pro (upgraded).",
        levels={
            "baseline": {
                "COORDINATOR_MODEL": "gemini-3.5-flash",
                "TRAVEL_MODEL": "gemini-3.5-flash",
                "EXPENSE_MODEL": "gemini-3.5-flash",
            },
            "upgraded": {
                "COORDINATOR_MODEL": "gemini-3.1-pro-preview",
                "TRAVEL_MODEL": "gemini-3.1-pro-preview",
                "EXPENSE_MODEL": "gemini-3.1-pro-preview",
            },
        },
    ),
    Factor(
        name="model_backend",
        channel="engine_env",
        description="Coordinator backbone: Gemini flash (baseline) vs Anthropic "
        "Claude Sonnet. Unlike model_tier (which moves all three model env vars "
        "together), this moves ONLY COORDINATOR_MODEL so the main effect isolates "
        "the coordinator model's effect while the sub-agents stay fixed. Level "
        "order (gemini -> claude) makes the DOE main effect claude_mean - "
        "gemini_mean per response variable. Powers the Gemini-vs-Claude bake-off "
        "(docs/notes/coordinator-model-bakeoff.md).",
        levels={
            "gemini": {"COORDINATOR_MODEL": "gemini-3.6-flash"},
            "claude": {"COORDINATOR_MODEL": "claude-sonnet-5"},
        },
    ),
    Factor(
        name="prompt_variant",
        channel="engine_env",
        description="Pre-GEPA baseline prompts vs GEPA-optimized prompts.",
        levels={
            "baseline": {"PROMPT_VARIANT": "baseline"},
            "gepa": {"PROMPT_VARIANT": "gepa"},
        },
    ),
    Factor(
        name="eval_fidelity",
        channel="param",
        description="Eval depth: quick (cheap) vs thorough.",
        levels={
            "quick": {"scenario_count": 3, "max_turns": 2},
            "thorough": {"scenario_count": 8, "max_turns": 4},
        },
    ),
    Factor(
        name="memory_bank",
        channel="engine_env",
        description="Vertex Memory Bank on the coordinator: off (no "
        "PreloadMemoryTool, no cross-session recall, engine wrapped session-only) "
        "vs on (managed Memory Bank). Measures the personalization/recall uplift "
        "against its cost + latency overhead.",
        levels={
            "off": {"ENABLE_MEMORY_BANK": "0"},
            "on": {"ENABLE_MEMORY_BANK": "1"},
        },
    ),
]

FACTORS_BY_NAME: dict[str, Factor] = {f.name: f for f in FACTORS}


def get_factors(names: list[str] | None = None) -> list[Factor]:
    """Return the requested factors (all, in registry order, if names is None)."""
    if names is None:
        return list(FACTORS)
    try:
        return [FACTORS_BY_NAME[n] for n in names]
    except KeyError as e:
        raise KeyError(f"unknown factor {e.args[0]!r}; known: {list(FACTORS_BY_NAME)}") from e


def requires_fresh_deploy(active_factors: list[Factor]) -> bool:
    """True if any active factor reconfigures the deployed engine (engine_env)."""
    return any(f.channel == "engine_env" for f in active_factors)
