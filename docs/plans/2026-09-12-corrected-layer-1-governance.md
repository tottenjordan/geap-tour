# Corrected Layer 1 Governance Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task.
>
> Gate every commit on `uv sync --all-groups && uv run --no-sync ruff format --check && uv run --no-sync ruff check && uv run --no-sync ty check src/ && uv run --no-sync pytest` (expect **1794** on `main`, rising as tasks add tests).
> **Every change lands via PR; never push to `main`, never merge without explicit approval.**
> **NO `Co-Authored-By` and NO "Generated with Claude Code" trailers** (owner reconfirmed 2026-09-11: `CODE_STANDARDS.md` wins). Git identity: Jordan Totten `<jordantotten@google.com>`.
> Never `git add -A`; never touch `notebooks/jt_eval_jw.ipynb`; never repoint `AGENT_ENGINE_ID`.
> On approval, also save this to `docs/plans/2026-09-12-corrected-layer-1-governance.md` (plan-mode only permits editing the plan file).
> **Stacks on PRs #119 and #120** — branch from `gateway-enable` once both land, or from `main` after they merge.

**Goal:** Make `setup_governance_policies.sh` Layer 1 apply real, evaluable per-tool IAM policies to the agents that actually exist — replacing three policy files that were written to `/tmp`, never applied, aimed at a removed architecture, and whose conditions could not evaluate.

**Architecture:** Three ordered pieces. (1) Declare MCP `ToolAnnotations` so the IAP CEL attributes exist at all. (2) Re-derive the policy set for the direct-tools topology — coordinator and router, each holding all three toolsets — with the correct per-engine SPIFFE principal. (3) Actually apply them, via the real `gcloud` invocation. Policies stay **audit-only** not by a DRY_RUN flag (none is reachable via `gcloud`) but because IAP enforces *at the Agent Gateway boundary* and no engine has `agentGatewayConfig` set — so attaching the gateway later is the single reversible "flip to enforce".

**Tech Stack:** FastMCP 3.4.7 (`mcp.types.ToolAnnotations`), Cloud Run, Identity-Aware Proxy CEL conditions, `gcloud beta iap web set-iam-policy`, Agent Registry.

---

## Context

`docs/notes/geap-services-audit-2026-09.md` and the Phase-3 reconnaissance (PR #120) found Layer 1 has **four independent defects**, each verified live:

1. **It never applies anything.** Three policy JSONs are written to `/tmp` and the only `set-iam-policy` in the 800-line script is inside an `info` string telling the operator to run it. It printed `ok "IAM policy created"` three times. PR #119 corrected the messages; this plan corrects the behaviour.
2. **Wrong principal.** `principal://${RE_SA}` where `RE_SA="service-…@gcp-sa-aiplatform-re.iam.gserviceaccount.com"` — the Reasoning Engine *service agent*, in SPIFFE syntax that fits neither a SPIFFE ID nor a service account. CLAUDE.md already documents this as the wrong-principal mistake. `grant_registry_read` (`scripts/setup_governance_policies.sh:279`) derives the right one, `principal://${eff}`, from the live engine spec.
3. **The conditions cannot evaluate.** They read `iap.googleapis.com/mcp.tool.isReadOnly` and `isDestructive`, but every tool is declared as a bare `@mcp.tool()` with **no annotations**. With `getAttribute(..., false)` defaults: `isReadOnly == true` never matches (**denies all search**) and `isDestructive == false` always matches (**constrains nothing**). Applying them as-is would break the demo, not govern it.
4. **Wrong architecture and a wrong tool name.** Policies assume Coordinator / Travel / Expense agents with one MCP server each. Travel and expense **are not deployed**; the coordinator has held all three toolsets directly since 2026-08-20. The allowlist also names `get_expenses`; the real tool is `get_user_expenses`.

Verified live: `gcloud beta iap web get-iam-policy --resource-type=agent-registry --mcp-server=<id> --region=us-central1` returns `etag: ACAB` — an empty policy. Nothing has ever been bound. Note the flag is `--mcp-server`, not the `--mcpServer` the script prints.

**Intended outcome:** a governance layer that is real, inspectable, and enforceable by one flag — instead of a printout that reported success.

---

## Task 1: Annotate the search tools

**Files:**
- Modify: `src/mcp_servers/search/server.py`
- Test: `tests/test_mcp_servers.py`

**Step 1: Write the failing test**

```python
class TestToolAnnotations:
    """IAP CEL conditions read these; absent hints make every condition misfire.

    `api.getAttribute('iap.googleapis.com/mcp.tool.isReadOnly', false)` returns the
    DEFAULT when the hint is absent, so `isReadOnly == true` never matches (denying
    a read-only tool) and `isDestructive == false` always matches (constraining
    nothing). The annotation is what makes the policy mean anything.
    """

    def test_search_tools_are_annotated_read_only(self):
        from src.mcp_servers.search import server

        for name in ("search_flights", "search_hotels"):
            ann = server.mcp._tool_manager._tools[name].annotations
            assert ann is not None, f"{name} has no ToolAnnotations"
            assert ann.readOnlyHint is True
            assert ann.destructiveHint is False
```

**Step 2: Run it and watch it fail**

Run: `uv run --no-sync pytest tests/test_mcp_servers.py::TestToolAnnotations -v`
Expected: FAIL — `search_flights has no ToolAnnotations`.

> If `mcp._tool_manager._tools` is not the accessor in FastMCP 3.4.7, find the real one first:
> `uv run --no-sync python -c "from src.mcp_servers.search import server; print([a for a in dir(server.mcp) if 'tool' in a.lower()])"`.
> Prefer a public accessor if one exists; pin whichever you use with a comment, since it is an internal.

**Step 3: Add the annotations**

```python
from mcp.types import ToolAnnotations

# Declared so IAP's CEL conditions have attributes to read. Both search tools query
# a mock DB: no writes, no deletes, same answer for the same args, no outside world.
@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
)
def search_flights(origin: str, destination: str, date: str | None = None) -> list[dict]:
```

Same block on `search_hotels`.

**Step 4: Run the test**

Run: `uv run --no-sync pytest tests/test_mcp_servers.py::TestToolAnnotations -v` → PASS

**Step 5: Commit**

```bash
git add src/mcp_servers/search/server.py tests/test_mcp_servers.py
git commit -m "feat(mcp): annotate the search tools so IAP conditions can evaluate"
```

---

## Task 2: Annotate the booking and expense tools

**Files:** `src/mcp_servers/booking/server.py`, `src/mcp_servers/expense/server.py`, `tests/test_mcp_servers.py`

Extend the same test class to assert the full 10-tool matrix, then implement. **The semantics matter — these drive real authorization decisions:**

| tool | readOnly | destructive | idempotent | note |
| --- | --- | --- | --- | --- |
| `search_flights`, `search_hotels` | ✅ | ❌ | ✅ | Task 1 |
| `book_flight`, `book_hotel` | ❌ | ❌ | **❌** | each call mints a new `booking_id` |
| `cancel_booking` | ❌ | **✅** | ✅ | the one genuinely destructive tool |
| `get_booking_details`, `list_all_bookings` | ✅ | ❌ | ✅ | |
| `submit_expense` | ❌ | ❌ | **❌** | mints a new `expense_id` every call — the non-idempotence the `receipt-audit` skill warns about |
| `check_expense_policy`, `get_user_expenses` | ✅ | ❌ | ✅ | |

`openWorldHint=False` throughout — every tool reads a mock DB, nothing reaches the internet.

Add one test that guards the set as a whole, so a new tool cannot ship unannotated:

```python
    def test_every_tool_is_annotated(self):
        """A new unannotated tool silently disables whatever policy governs it."""
        for mod in ("search", "booking", "expense"):
            server = importlib.import_module(f"src.mcp_servers.{mod}.server")
            for name, tool in server.mcp._tool_manager._tools.items():
                assert tool.annotations is not None, f"{mod}.{name} is unannotated"
                assert tool.annotations.readOnlyHint is not None, f"{mod}.{name} readOnlyHint unset"
```

**Commit:** `feat(mcp): annotate the booking and expense tools`

---

## Task 3: Redeploy the MCP servers

**Files:** none — this is a live Cloud Run deploy.

The annotations are inert until the running servers advertise them. Cloud Run only; no Agent Engine is touched.

```bash
uv run python -m src.deploy.deploy_mcp_servers
uv run python -m src.eval.verify_mcp_tools --json    # all three PASS, 10 tools
```

**Then verify the annotations actually reach the wire** — the point of the whole task:

```bash
uv run --no-sync python -c "
import asyncio
from src.registry import get_mcp_tools
from src.config import SEARCH_MCP_SERVER
async def main():
    ts = get_mcp_tools(SEARCH_MCP_SERVER)
    for t in await ts.get_tools():
        print(t.name, getattr(t, 'annotations', None))
asyncio.run(main())"
```

Expected: non-`None` annotations with `readOnlyHint=True`. **If they are `None` over the wire, stop** — the rest of the plan cannot work, and the finding (FastMCP does not propagate annotations through this path) is more valuable than pressing on.

**No commit** — deploy only. Record the outcome in the Task 7 note.

---

## Task 4: Re-derive the policy set for the real topology

**Files:** Modify `scripts/setup_governance_policies.sh` (the Layer 1 block, ~lines 318–390)

Replace three agent→server policies with the pairs that exist. Both the coordinator and the router hold all three toolsets, so both need all three servers.

Reuse `grant_registry_read`'s effectiveIdentity read (`:279`) — do **not** write a second fetcher:

```bash
# The principal is the ENGINE's SPIFFE identity, read off the live spec. Granting
# the Reasoning Engine service agent (the old `principal://${RE_SA}`) is the
# wrong-principal mistake CLAUDE.md documents: egress IAM is evaluated against the
# agent identity, and a role on the RE service agent buys nothing.
_engine_identity() {
    curl -s -H "Authorization: Bearer ${ACCESS_TOKEN}" \
        "https://${REGION}-aiplatform.googleapis.com/v1/projects/${PROJECT_ID}/locations/${REGION}/reasoningEngines/$1" \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('spec',{}).get('effectiveIdentity',''))"
}
```

Conditions, now that the attributes exist:

- **search** — `isReadOnly == true`. True of both search tools; blocks anything non-read-only added later.
- **booking** — `isDestructive == false` **plus** an explicit `cancel_booking` allowance for both agents. Do **not** deny cancel: two `ROUTER_EVAL_CASES` expect `booking_mcp_cancel_booking`, and the coordinator's instruction was deliberately extended on 2026-08-21 to cover booking management.
- **expense** — `mcp.toolName in ['submit_expense', 'check_expense_policy', 'get_user_expenses']`. Fixes `get_expenses`, which does not exist.

**Commit:** `fix(governance): re-derive Layer 1 for the direct-tools topology`

---

## Task 5: Actually apply the policies

**Files:** `scripts/setup_governance_policies.sh`

Replace the printed instruction with the real call. **The flag is `--mcp-server`, not `--mcpServer`** — verified: the latter does not exist.

```bash
apply_iap_policy() {   # label, policy_file, mcp_server_resource_name
    local short; short="$(basename "$3")"
    run_cmd gcloud beta iap web set-iam-policy "$2" \
        --resource-type=agent-registry --mcp-server="${short}" \
        --region="${REGION}" --project="${PROJECT_ID}" \
        && ok "${1}: policy applied" \
        || { warn "${1}: apply FAILED — Layer 1 is not in force for this pair"; return 1; }
}
```

Then state the enforcement posture honestly in the script's output, because it is the subtle part:

```bash
info "Layer 1 policies are APPLIED but NOT YET ENFORCED. IAP evaluates at the Agent"
info "Gateway boundary and no engine has agentGatewayConfig set, so no traffic"
info "traverses a gateway. Inspect them with:"
info "  gcloud beta iap web get-iam-policy --resource-type=agent-registry --mcp-server=<id> --region=${REGION}"
info "Enforcement is one reversible flag: ENABLE_AGENT_GATEWAY=1 + an in-place --update."
```

> **Why not DRY_RUN:** the research names `iamEnforcementMode: DRY_RUN`, but `gcloud iap settings` exposes no enforcement flag and `agent-registry` is not a valid `--resource-type` there. The audit-only property comes from the gateway being unattached, which is real and verifiable — do not claim a DRY_RUN we did not set.

**Commit:** `feat(governance): apply Layer 1 policies instead of printing the command`

---

## Task 6: Guard the whole thing with tests

**Files:** `tests/test_no_hardcoded_values.py` (or a new `tests/test_governance_policies.py`)

The script is shell and never executes in CI, so tests read it as text — the pattern `test_no_hardcoded_values.py` already uses.

```python
class TestLayer1IsRealAndCorrect:
    """Four defects shipped here at once; each gets a guard.

    Layer 1 wrote policies to /tmp, printed "IAM policy created", targeted the RE
    service agent, and used conditions no tool could satisfy. Nothing executes this
    script in CI, so these are text assertions — weak, but they are what would have
    caught three of the four.
    """

    SCRIPT = (pathlib.Path(__file__).resolve().parents[1]
              / "scripts" / "setup_governance_policies.sh").read_text()

    def test_it_applies_and_does_not_merely_print(self):
        assert "gcloud beta iap web set-iam-policy" in self.SCRIPT
        assert "apply_iap_policy" in self.SCRIPT

    def test_it_uses_the_real_gcloud_flag(self):
        """--mcpServer is not a flag; the real one is --mcp-server."""
        assert "--mcpServer" not in self.SCRIPT

    def test_it_never_grants_the_RE_service_agent(self):
        assert "principal://${RE_SA}" not in self.SCRIPT

    def test_the_expense_allowlist_names_real_tools(self):
        from src.mcp_servers.expense import server

        for name in server.mcp._tool_manager._tools:
            pass  # tool names come from the server, not a literal list
        assert "'get_expenses'" not in self.SCRIPT, "get_expenses does not exist"
        assert "get_user_expenses" in self.SCRIPT
```

**Commit:** `test(governance): guard the four defects Layer 1 shipped with`

---

## Task 7: Document it

**Files:** `docs/notes/geap-services-audit-2026-09.md` (extend), `docs/notes/README.md` (index is at **199 of 200** — reclaim a line first)

Record what changed, and — per the repo's habit — **any negative result**, especially if Task 3 found annotations do not survive the wire.

**Commit:** `docs: record the corrected Layer 1`

---

## Verification

```bash
uv sync --all-groups && uv run --no-sync ruff format --check && uv run --no-sync ruff check \
  && uv run --no-sync ty check src/ && uv run --no-sync pytest -q

# Annotations reach the deployed servers (Task 3's gate)
uv run python -m src.eval.verify_mcp_tools --json

# Policies are really bound — was `etag: ACAB` (empty) before
gcloud beta iap web get-iam-policy --resource-type=agent-registry \
  --mcp-server="$(basename "$SEARCH_MCP_SERVER")" --region=us-central1 --project=hybrid-vertex

# Nothing is enforced yet: no engine carries agentGatewayConfig
uv run python -m src.deploy.verify_engine_config      # gateway_attached: "not requested"

# The demo still works end to end
uv run python -m src.eval.multi_agent_batch_eval --agents coordinator_agent --limit 8
```

**Success:** every tool advertises annotations; `get-iam-policy` returns real bindings against `principal://<effectiveIdentity>`; `verify_engine_config` still reports 0 critical; and the coordinator batch eval is unchanged, because nothing is enforced yet.

## Caveats

* **Task 3 is the load-bearing unknown.** If FastMCP annotations do not survive to the wire, Tasks 4–5 cannot work as written and the fallback is tool-name allowlists only (which need no annotations). Find out before writing policy code.
* **Applied ≠ enforced, and the script must say so.** The single most likely misreading of this work is that governance is now live. It is not until a gateway is attached.
* **Do not deny `cancel_booking`.** Two router eval cases expect it and the coordinator's instruction grants it deliberately.
* **Cloud Run redeploy only.** No Agent Engine is updated anywhere in this plan.
* Layer 2 (SGP) is out of scope.
