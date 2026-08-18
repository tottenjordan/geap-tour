# Agent Engine `stream_query` SSE-parse skew (and the raw-SSE fallback)

## Symptom

Every SDK `stream_query` against the recycled demo probe engine
(`4380288848559603712`) raises during iteration:

```
ValueError: Can only parse array of JSON objects, instead got {
```

This hits **all** SDK-based `stream_query` tooling: the online monitor, the
traffic generator, tool-faithfulness capture, `demo_readiness engine_live`, and
the demo notebooks. It is **systemic and deterministic** — every recycle of the
engine reproduces it.

## Root cause — a client/server streaming-format skew (NOT a broken engine)

The engine is **healthy**. Verified live: a raw HTTP POST to
`:streamQuery?alt=sse` returns **HTTP 200** with a real `gemini-2.5-flash` answer
and a full `function_call` trajectory. Two independent confirmations on
2026-08-18:

- SDK path raises the exact skew ValueError (classified by
  `raw_stream.is_sse_parse_skew`).
- Raw path against the same engine returns real content (196 chars).

The mismatch is entirely client-side:

- The installed `google-api-core` is **2.34.0** — the **latest available**
  (`>=2.35` is unsatisfiable in this environment). Its REST streaming parser
  (`google.api_core._rest_streaming_base._process_chunk`) is **array-only**: it
  raises unless the HTTP body starts with `[`.
- The recycled engine streams **newline-delimited JSON objects** via
  `:streamQuery?alt=sse` — each line a complete `{...}` event (no `data:`
  prefix, no enclosing array).

So the array parser chokes on the first `{`. There is **no SDK flag** to switch
the parser and **no version to upgrade to**. The console/UI, A2A, and raw REST
are all unaffected — only the SDK's array parser is.

## Fix — client-only raw-SSE reader with transparent fallback

`src/eval/raw_stream.py` (client-only; **no redeploy**, the served engine is
untouched):

- `create_session(resource_name, user_id)` → POST `:query`
  (`class_method=create_session`) → session id.
- `stream_query_events(resource_name, *, message, user_id, session_id)` → POST
  `:streamQuery?alt=sse` (`class_method=stream_query`), parse **object-per-line**,
  yield the **same event dicts** the SDK would have (`{"content": {"parts":
  [...]}, "author": ...}`). So `generate_traffic._extract_text` and every
  `trajectory_eval` extractor consume them **unchanged**.
- `capture_pairs` / `capture_triples` — drop-in `(prompt, response)` and
  `{prompt, response, actual_trajectory}` captures.
- `parse_sse_line` tolerates an optional `data:` prefix, blank lines, and
  `[DONE]`; only top-level **objects** are events (a bare `[...]` line is skipped).
- Region is read straight from the resource name (`_endpoint_base`), so the
  endpoint and the engine always agree — engines are **regional**, never the
  `global` model endpoint.
- Auth is ADC (`google.auth.default()` + refresh); `post`/`token` are injectable
  so the whole path is unit-tested with **no GCP** (`tests/test_raw_stream.py`).

Two shared classifiers live here too and are reused by every consumer:
`is_sse_parse_skew(exc)` (a `ValueError` whose message contains
`SSE_PARSE_MARKER`, so unrelated `ValueError`s still propagate) and
`agent_resource_name(agent)` (pulls the full resource name off an SDK engine
handle).

### Wired-in consumers (SDK first, raw-SSE on the skew)

Each catches only the SSE-skew `ValueError` and falls back; anything else
re-raises unchanged:

- `online_monitor.capture_live_interactions` and `capture_live_faithfulness`
  → `demo_readiness engine_live` inherits the fix (no longer a false negative;
  a red row there now means a genuine empty-at-200 / wedge).
- `tool_faithfulness.capture_interaction`.
- `traffic.generate_traffic._send_single_query` (the steady-traffic path). The
  **multi-turn conversation** loop deliberately keeps the SDK path to preserve
  session continuity (the raw fallback opens a fresh session per call); if the
  skew hits there it counts errors as before, never crashing.

## Verification

```bash
uv run pytest tests/test_raw_stream.py tests/test_online_monitor.py -q   # offline
uv run python -m src.eval.demo_readiness --engine-id 4380288848559603712 # engine_live PASS via fallback
```

## Caveats

- The fallback fetches one ADC token and reuses it across a capture's prompts.
- The demo notebooks still call the SDK directly; they'd hit the skew on a
  recycled engine unless routed through `raw_stream` (follow-up if needed).
- If a future `google-api-core` gains an NDJSON-aware parser, the fallback
  becomes dead code (the SDK path would just succeed) — harmless, and the
  classifier keeps it from masking unrelated errors in the meantime.
