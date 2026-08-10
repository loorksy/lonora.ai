# Trading Domain Extension — Implementation Progress

Tracks implementation of the Forex/trading agent platform extension described
in the approved plan (audit + plan approved in-session; see git log on
`claude/synkora-trading-audit-n6aeo1`). Read this file first when resuming
work — it records what's done, what's verified, what's deferred, and key
architectural decisions made along the way.

**Ground rules carried through every phase** (from the approved plan):
- Extend Synkora's existing agent/tool/MCP/RBAC/HITL/audit infrastructure —
  never duplicate it.
- Backend stays Python/FastAPI/SQLAlchemy/Celery (Synkora's stack). Frontend
  stays Next.js/TypeScript (Synkora's stack). We do **not** reuse any code
  from the `loorksy/AiChart` reference repo (Node/OpenAI-SDK agent runtime)
  — only its UX/IA patterns (workspace split view, tabbed permission-gated
  admin console, approval-card flow, MT5 linking wizard) are used as a
  design reference for later frontend phases.
- No tool ever executes a real-money action directly — everything gated
  through TradeProposal → AgentApprovalRequest → human approval →
  execution, per §9 of the spec.
- Every phase: implement, verify (lint/compile + targeted logic tests +
  migration round-trip where relevant — full `pytest` is not runnable in
  this sandbox, see "Environment note" below), commit, update this file.

## Environment note

This sandbox cannot run the full `pytest` suite or full `uv sync` — an
unrelated pre-existing dependency (`xmlsec`, used only by SAML) fails to
build here, and there's a broken system `cryptography`/`cffi` install that
breaks any import chain touching `jwt`/`cryptography` transitively via
`src/services/__init__.py`'s eager imports. This is an environment
limitation, not a code issue. Verification per phase instead uses: `ruff
check`/`ruff format --check` (works fully), `py_compile` on all touched
files, isolated module-level logic tests (stub the `src.*` package tree,
`importlib` the target file directly, exercise its functions/classes with
real or mocked inputs — this bypasses the broken heavy import chain while
still executing real code), and a real local Postgres instance for migration
round-trips (works fully, verified in Phase 1). **Before merging, run the
full `pytest` suite and `alembic upgrade head` in a real dev/CI environment**
— this file will flag anywhere that's especially important to double-check.

## Phase status

| Phase | Status | Notes |
|---|---|---|
| 1. Foundation (models, migration, broker adapters, credentials) | ✅ Done | Commit `4b6bcfa` |
| 2. Read-only trading tools | ✅ Done | Commit pending (see below) |
| 3. Trading Agent (config, system prompt, tool allow-list) | ✅ Done | |
| 4. Trading charts (klinecharts, web) | ✅ Done | |
| 5. Approval-gated proposals (risk engine, position sizing, RBAC, execution) | ⬜ Not started | |
| 6. Telegram + WhatsApp (buttons, chart images) | ⬜ Not started | |
| 7. MCP Apps / ext-apps investigation | ⬜ Not started | |
| 8. Production hardening (observability, docs, rate limiting) | ⬜ Not started | |

---

## Phase 1 — Foundation ✅ (commit `4b6bcfa`)

**Models** (`api/src/models/`): `TradingAccount`, `RiskConfiguration`,
`TradeProposal` (+ `TradeProposalStatus` enum), `TradeExecution`,
`PlatformBrokerCredential`. All registered in `models/__init__.py`.

**Enum extensions**: `ApprovalStatus` (`agent_approval.py`) gained
`EXECUTING`/`FAILED` (closes a real gap — that transition was never wired
up in the existing HITL system). `ActivityType` (`activity_log.py`) gained
`TRADING`.

**Migration**: `api/migrations/versions/20260810_0001_add_trading_domain.py`,
`down_revision = "95b0831fc354"` (the actual head — its filename is
`20260701_0001_context_file_load_mode.py`; the codebase's revision ids and
filenames diverge for older migrations, verified via full AST-parse of the
migration DAG, not just filename sort). Verified with a full
upgrade→downgrade→upgrade round-trip against real local Postgres 16.

**Broker adapters** (`api/src/services/trading/`):
- `brokers/base.py` — `MarketDataAdapter`/`ExecutionBrokerAdapter` ABCs +
  shared dataclasses (`Quote`, `Candle`, `Position`, `Order`, etc.)
- `brokers/oanda_client.py` — OANDA v20 REST (practice/live), platform
  credential only, no execution.
- `brokers/metaapi_client.py` — MetaApi REST Client API (RPC, no
  websocket/streaming) + Provisioning API for account linking/region
  lookup. **Verify against a real MetaApi demo account before production**:
  the history-orders/history-deals time-range paths follow the documented
  naming convention of their confirmed siblings but weren't independently
  confirmed byte-for-byte (this sandbox's egress proxy blocks
  `metaapi.cloud` directly; confirmed endpoints via search-engine results
  instead — see file docstring for exactly which paths are "confirmed" vs
  "follows the pattern").
- `symbol_mapping.py` — explicit OANDA↔MetaApi symbol table + flagged
  heuristic fallback + broker-suffix resolver.
- `timeframes.py` — canonical timeframe validation shared by both adapters.
- `broker_credential_service.py` — the only place that reads
  `PlatformBrokerCredential`/`TradingAccount` secrets and builds a ready
  adapter instance.

**Design correction made mid-phase**: initially put OANDA's account id
inside the encrypted token string (`"accountId:token"`) — fixed to a proper
plain `account_ref` column on `PlatformBrokerCredential` (account ids
aren't secret, don't belong inside the encrypted blob). Migration and model
both updated before commit.

**Verified**: `ruff check`/`ruff format --check` clean on every touched
file. All files `py_compile` clean. Migration round-tripped twice against
real Postgres. Both broker adapters exercised with `httpx.MockTransport`
end-to-end (auth headers, URL/region construction, request body shape,
response parsing) — not just import-checked. `symbol_mapping.py` logic
unit-tested directly (explicit table, heuristic fallback, ambiguous-suffix
case).

---

## Phase 2 — Read-only trading tools ✅

**Service** (`api/src/services/trading/market_data_service.py`): the single
source-aware read layer. Every function takes an explicit `source`
("oanda" | "metaapi"), and every MetaApi-backed call resolves and verifies
the `TradingAccount` first — checks `tenant_id` match (hard isolation) and,
by default, `account_id` (ownership) match before ever calling MetaApi.
Never mixes OANDA and MetaApi data in one response.

**Tools** (`internal_tools/trading_tools.py` +
`tool_registrations/trading_tools_registry.py`, wired into
`adk_tools.py::_register_default_tools`, same pattern as every other
integration — no new tool registry): `internal_trading_list_accounts`,
`_get_quote`, `_get_candles`, `_get_instruments`, `_get_symbol_information`,
`_get_account_information`, `_get_positions`, `_get_orders`,
`_get_trade_history`, `_calculate_margin`. None are tagged
`tool_category="action"` — all safe to call without approval. `_context_ids()`
pulls `tenant_id`/`user_id` out of `RuntimeContext` (handles both the
dataclass and the dict-shaped form some call sites use), matching the
convention in every other `internal_tools/*.py` file.

**Verified**: `ruff check`/`format --check` clean, `py_compile` clean on
all 4 touched/new files (the one-line addition to `adk_tools.py` doesn't
introduce any new lint errors — confirmed by diffing ruff output against
the pre-existing-on-main version of that file, which already had 5
unrelated pre-existing lint findings elsewhere in the file). Functionally
tested `market_data_service.py` with a stubbed `broker_credential_service`
(fake OANDA/MetaApi clients + fake `TradingAccount` rows) exercising: OANDA
quote (no account needed), MetaApi quote for the account's own owner,
**MetaApi quote rejected when `requesting_account_id` doesn't own the
account**, **cross-tenant access rejected**, account-information shape, and
unsupported-source rejection. The two isolation checks are the
security-critical path for this phase and were explicitly exercised, not
just read over.

---

## Phase 3 — Trading Agent configuration ✅

**System prompt** (`api/src/services/agents/trading_agent_prompt.py`):
`TRADING_AGENT_SYSTEM_PROMPT` — data discipline (always cite source, never
fabricate), recommendation structure (direction/entry/SL always
required/TP/evidence/source+timestamp/uncertainty), and an explicit section
telling the model it can never execute directly or approve its own
proposal, no matter how the request is phrased. Docstring is explicit that
none of this is a security boundary — that's still 100% enforced in code
(risk_engine.py/position_sizing.py/HITL, landing in Phase 5).

**Tool category**: added `"trading_tools": ["internal_trading_*"]` to both
`TOOL_CATEGORY_TO_PATTERNS` and `PLATFORM_TOOL_CATALOG` in
`internal_tools/platform_tools.py` — the same mechanism every other
integration uses to appear as a pickable bundle in the Agent Builder UI's
tool picker, and to expand into concrete tool names on creation. Verified
the glob pattern matches exactly the 10 Phase 2 tool names and nothing
else.

**Seed script** (`api/seed_trading_agent.py`): creates/updates the "Trading
Agent" for a given `--tenant-id`. Deliberately does **not** reimplement
agent-creation logic — imports and reuses
`_resolve_primary_llm_config`/`_resolve_requested_agent_tools` directly
from `controllers/agents/index.py` (the same functions the real
`POST /api/v1/agents` endpoint uses), so the seeded agent has identical
shape/LLM-config-fallback behavior to one created by a human through the
Agent Builder. Idempotent: re-running updates the existing agent's prompt/
description/tools in place instead of creating a duplicate. LLM credentials
are never hand-rolled — either passed via `--api-key`/`--provider`/`--model`
or inherited from the tenant's Platform Engineer agent config, exactly like
the UI path.

**Verified**: `py_compile`/`ruff check`/`ruff format --check` clean. The
category→tool-name glob-matching logic (the part with real behavioral
risk) was functionally tested in isolation. **Not runnable in this
sandbox** (needs the full app + a real Postgres + an existing tenant/account
+ a real or inherited LLM API key): the script itself, end-to-end. Run
`python seed_trading_agent.py --tenant-id <uuid>` in a real environment
before relying on it, and confirm the created Agent shows up correctly in
the Agent Builder UI with all 10 trading tools attached.

---

## Phase 4 — Trading charts (klinecharts) ✅

**Data model** (`web/lib/types/trading.ts`): `TradingChartData` (symbol,
timeframe, source, `bars: OHLCBar[]`, `overlays: {entry, stopLoss,
takeProfit[], supportResistance[], trendlines[]}`) — kept independent of
klinecharts' own types, per the plan's "keep chart data model independent
from the rendering library" requirement.

**Renderer** (`web/components/charts/renderers/KLineChartsRenderer.tsx`):
wired into `ChartRenderer.tsx`'s existing `chart.library` switch as a new
`'klinecharts'` case, alongside `chartjs`/`recharts`/`plotly` — no changes
to the existing three paths, confirmed by re-running `tsc --noEmit` clean
across the whole web app.

**Important API-version finding**: klinecharts v10 (the current npm
`latest`, installed here) is not the same imperative `applyNewData(...)`
API most tutorials/blog posts describe (that's v9). v10 uses a
**DataLoader** pattern (`chart.setDataLoader({ getBars: ({callback}) =>
callback(data, hasMore) })`) plus `setSymbol`/`setPeriod`. This session had
no live access to klinecharts.com (blocked by the sandbox's egress proxy)
to confirm this from the docs site, so the implementation was built by
downloading the actual npm package (`npm pack klinecharts@10.0.2`) and
reading its bundled `dist/index.d.ts` directly — the authoritative source.
Confirmed overlay type names by grepping the bundled JS for known
identifiers: `priceLine` (used for entry/SL/TP — a labeled horizontal
line), `horizontalStraightLine` (support/resistance), `segment` (trendlines,
2 points).

**Verified — this one got a real browser test, not just lint**: `pnpm
install` (klinecharts 10.0.2 resolved cleanly), `tsc --noEmit` clean across
the whole app, `eslint` clean on all 3 touched/new files. Then: wrote a
temporary standalone Next.js page rendering `<ChartRenderer>` with 120
generated candles + entry/SL/TP/support-resistance/trendline overlays,
started the real `next dev` server, loaded the page in headless Chromium
via Playwright, and confirmed **zero console/page errors**, 10 canvas
elements rendered (klinecharts' multi-layer canvas architecture — main
pane + volume pane + overlay layers), and visually confirmed via screenshot
that candlesticks, the VOL/MA volume sub-pane, and all overlay price lines
with their colored labels rendered correctly. The test page was then
deleted — it was scaffolding only, not a real route.

**Known follow-up, not blocking**: `ChartRenderer.tsx`'s PNG-download
button grabs the first `<canvas>` it finds in the container, which works
fine for Chart.js (single canvas) but will only capture klinecharts' first
internal layer, not the composited full chart. klinecharts exposes
`chart.getConvertPictureUrl()` for a correct full-chart export; wiring that
in would need an imperative handle/ref from `KLineChartsRenderer` up to
`ChartRenderer` — left as a follow-up since it's a cosmetic gap (download
still produces *a* PNG, just possibly a partial one), not a functional
break, and this phase's scope was chart rendering, not export tooling.

---
