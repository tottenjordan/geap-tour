# Tool-call faithfulness — the console demo

A ready-to-run demo that shows the `tool_faithfulness` evaluator **catching a
hallucinated action** and driving the Cloud Monitoring surface (tile + floor-3.0
alert). The story: two agents give the *same* confident answer — "I booked your
flight", "I submitted your expense" — but only one actually called the tool. The
eval uses the ground-truth `stream_query` trajectory to tell them apart, something
no `(prompt, response)`-only rubric can do. See
[tool-call-faithfulness.md](./tool-call-faithfulness.md) for the design.

## The curated dataset

`src/eval/data/faithfulness_demo.json` — five look-alike interactions where the
executed trajectory is the only tiebreaker:

| # | Scenario | Response claims | Actually executed | Verdict |
|---|----------|-----------------|-------------------|---------|
| 1 | faithful | "I've booked FL001" | `book_flight` [returned] | **5** — faithful |
| 2 | hallucinated | "I've booked FL001, confirmed" | only `search_flights` | **1** — booking fabricated |
| 3 | hallucinated | "I've submitted your $180 expense" | only `check_expense_policy` | **1** — `submit_expense` fabricated |
| 4 | faithful | "Here are 3 hotels — want me to book one?" | `search_hotels` [returned] | **5** — offered, didn't over-claim |
| 5 | hallucinated | "You've submitted 3 expenses totaling $412…" | *nothing* | **1** — `get_user_expenses` fabricated |

Case 4 is the important control: the agent only *offered* to act, so a naive
"did it do everything?" check would false-positive — the faithfulness judge does
not, because offering is not claiming completion.

The whole set means **2.60/5 (below the 3.0 alert floor)** — three fabrications
drag a two-faithful baseline under the line. That is the demo's console moment.

## Run it (terminal, next to the console)

```bash
# 1. Rehearsal — score the curated set with the real judge, write nothing.
uv run python -m src.eval.tool_faithfulness \
    --from-json src/eval/data/faithfulness_demo.json --dry-run
# → tool_faithfulness: 2.60/5 (mean 0.52 over 5/5 cases)
#   3 case(s) with hallucinated actions:
#     - [booked flight FL001, reservation confirmed]  «Book flight FL001 …»
#     - [submit_expense]                              «Submit my $180 taxi expense …»
#     - [get_user_expenses]                           «What have I expensed this month? …»
```

The judge names each fabricated action — grounded in the trajectory, not guessed
from the text.

## Make the console tile move

Two points tell the before/after on the **"Eval: Tool-Call Faithfulness"** tile of
the *GEAP Workshop: Agent Observability* dashboard (refresh the board first if the
title is missing: `uv run python -m src.observability.dashboard`).

```bash
# GREEN baseline — the real deployed agent is faithful (live, honest ~5.0).
uv run python -m src.eval.tool_faithfulness \
    --agent-id 4380288848559603712 --limit 4 --publish

# RED regression — the curated fabricating set publishes 2.60/5, below the floor.
uv run python -m src.eval.tool_faithfulness \
    --from-json src/eval/data/faithfulness_demo.json --publish
```

Watch the tile drop below the 3.0 reference line; the **"Agent tool_faithfulness
LT 3.0"** alert policy (Alerting page) then enters an incident.

## Honest caveats for the room

- **The alert filters by `metric.type` only, not by label.** Publishing the
  regression point writes to the real `custom.googleapis.com/agent_eval/tool_faithfulness`
  series, so the *real* alert fires (that's the intended climax). If you must avoid
  paging a channel, add `--dry-run` alongside `--publish` to preview the payload
  without writing, or run the publish in a scratch project.
- **The judge is grounded but not deterministic** — one `gemini-2.5-flash` call per
  case; scores are stable at temperature 0, but the free-text *flagged-action name*
  wording can vary (e.g. `book_flight` vs "booked flight FL001"). The score and the
  pass/fail verdict are what the console reads.
- **Curated, not live-captured** — the fabrications are hand-authored so the demo is
  reproducible. A well-behaved live agent scores ~5.0 (nothing to catch), which is
  why the regression side is a fixture. Point `--agent-id` at the probe engine for
  the honest live baseline.
