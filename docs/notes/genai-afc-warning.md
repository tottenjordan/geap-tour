# The google-genai AFC warning (and why every config now disables it)

**Status:** shipped 2026-08-20. One-line change at every `GenerateContentConfig`
this repo builds, behind one shared helper.

## The symptom

The deployed **router** (`6134089059699523584`) flooded Cloud Logging with:

```
WARNING: Direct use of automatic function calling (AFC) in AsyncModels.generate_content
is not recommended. Instead, we recommend to use AFC in AsyncChat.send_message. …
INFO:    AFC is enabled with max remote calls: 10.
```

Measured 2026-08-20 before the fix:

| query | result |
| --- | --- |
| `textPayload:"Direct use of automatic function calling"`, 2d, limit 200 | **200/200 rows, all router `6134089059699523584`** |
| same, 6h, limit 1000 | **1000 rows — query cap hit**, spread across many worker PIDs |
| same, scoped to coordinator probe `4380288848559603712` | **zero hits** |
| 1h window at 19:18Z (the immediate before/after baseline) | **46 WARNING + 55 INFO rows**, all router |

## Root cause

`google/genai/_extra_utils.py:should_disable_afc(config)` returns `False` unless
the caller *explicitly* sets `automatic_function_calling.disable=True` — `if not
config: return False`, then "Default to enable AFC if not specified". So every
`generate_content` / `generate_content_stream` call takes the AFC branch, which
logs the `INFO` line **per call** and the `WARNING` **once per class per process**
(`AsyncModels._logged_afc_warning`, upstream `models.py:8710` and `:8956`). The
volume is high because the managed runtime spreads requests across many worker
processes and each one logs the warning once.

There are **two** emitters, and fixing one alone only changes the wording (the
two async messages differ only in which method they name first):

1. **Our own classifier** — `src/router/complexity.py:classify_complexity` calls
   `client.aio.models.generate_content(...)` from `before_agent_callback` on
   **every** router request. It runs before any model call, which is why the
   observed wording was the non-stream (`AsyncModels.generate_content`) variant in
   every process.
2. **ADK itself** — ADK never sets `automatic_function_calling`, and
   `google/adk/flows/llm_flows/basic.py` does
   `llm_request.config = agent.generate_content_config.model_copy(deep=True)`, so
   whatever the *agent* carries is exactly what reaches
   `google/adk/models/google_llm.py` (`generate_content_stream` / `generate_content`).

## Why disabling AFC is safe

ADK runs its **own** function-calling loop and passes tool *declarations*
(`types.Tool`), never Python callables. `_extra_utils.get_function_map()` is
therefore always empty, so the AFC loop makes one `_generate_content` call and
breaks — behaviourally identical to the disabled path. Disabling it skips a
per-call `model_copy(deep=True)` and, on the streaming path, an extra
`model_output` accumulation inside an open `AsyncExitStack`. It does **not**
disable ADK tool calling.

## The fix

`src/models/afc.py:with_afc_disabled(config=None)` returns a copy of a config
(or a fresh one) carrying `AutomaticFunctionCallingConfig(disable=True)`. One
helper, no duplicated literal, no shared mutable pydantic object. Applied at:

- `src/router/complexity.py` — the classifier call.
- `src/armor/config.py:get_armored_generate_config` — **both** return branches,
  which is how the coordinator gets it (`coordinator_agent.py`).
- `src/router/agents.py` — `router_agent` and `_build_tier_agent` (the five
  lazily-built tier agents).
- `src/agents/{travel,expense,lite,flash,pro,sonnet,opus}_agent.py` — one
  `generate_content_config=with_afc_disabled()` kwarg each.
- `src/eval/judge_client.py` — the sync judge path (`Models._logged_afc_warning`
  is a separate class flag). Consistency and a cheaper call; the judges run in
  our own processes, so this was never the production log source.

A parametrized guard test (`tests/test_standalone_agents.py`,
`tests/test_agents.py`, `tests/test_router.py`, `tests/test_armor.py`) fails if a
new agent ships without the stamp.

## Traps

- **It is invisible locally.** The warning string was added in **google-genai
  2.18.1**; the dev venv is on **2.17.0**, so `grep` in `.venv` finds nothing.
  Deployed engines get ≥2.18.1 through the unbounded `google-genai>=2` pin in
  `src/deploy/deploy_agents.py:REQUIREMENTS`. Every test therefore asserts on
  config *contents*, never on log output.
- **Deliberate non-action:** no upper bound was added on `google-genai`. It is
  already transitively capped `<3` by `google-adk==2.6.3` and
  `google-cloud-aiplatform`; pinning away from a minor release would cost
  upstream fixes for a cosmetic warning.
- **This is log hygiene, not a bug fix.** No user-visible behaviour changes. Do
  not claim a latency win without measuring one.
- **If upstream flips the default,** these stamps become redundant but stay
  harmless — they are explicit intent, not a workaround to rip out.

## Result

Both engines were redeployed **in place** (router with the mandatory tier
overrides — a plain `deploy_agents router --update` regresses the tiers to
Gemini-3; coordinator probe via `src.doe.deploy_coordinator --update`), then
driven with fresh traffic so new worker processes would re-log if the fix had
not landed: 56 router queries (0 errors), plus ~15 min of coordinator traffic,
`verify_cross_session_recall`, and a 6-interaction `online_monitor --dry-run`.

Before/after, counted with the same two queries:

| window | `Direct use of automatic function calling` | `AFC is enabled with max remote calls` |
| --- | --- | --- |
| 2h ending 19:38Z (pre-fix traffic) | **157** rows, all router `6134…` | **195** rows, all router |
| since 19:28Z (post-redeploy traffic only) | **0** | **0** |

The last pre-fix row of either kind is `2026-08-20T18:25:53Z`. Absence is not
ingestion lag: over the same post-redeploy window the router had 1000+ rows
ingested (newest `19:47:54Z`) and the coordinator probe 1000+ (newest
`19:53:05Z`) — the engines were logging, just not this.

Behaviour is unchanged: `verify_mcp_tools --json` resolves all 10 tools across
the three domains, `verify_cross_session_recall --user-id alice` prints
`RECALL: PASS`, and the online monitor scored 6/6 captured interactions
(helpfulness 4.4, tool-use 4.8, policy 3.4 — within the usual small-sample band).
