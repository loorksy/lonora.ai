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
| 5. Approval-gated proposals (risk engine, position sizing, RBAC, execution) | ✅ Done | See below — includes a pre-existing platform bug fix |
| 6. Telegram + WhatsApp (buttons, chart images) | ✅ Done | Includes a minimal web dashboard page |
| 7. MCP Apps / ext-apps investigation | 🔍 Investigated — deferred | Not implemented; see rationale below |
| 8. Production hardening (observability, docs, rate limiting) | ✅ Done | Closes a real deployment gap (credential seeding) |

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

## Phase 5 — Approval-gated proposals ✅

The biggest phase — this is the actual money-safety layer. Summary by
piece, then the bugs this phase's testing caught (including one **pre-existing
platform bug**, not introduced by this work, that had to be fixed for the
new code to function at all).

**risk_engine.py** — pure `validate_trade_request()`: trade_allowed,
symbol tradeable, stop-loss required for new positions, volume vs
symbol min/max/step, order-type/limit-price consistency, SL/TP distance +
correct-side sanity, margin sufficiency, max position size, max risk %,
max open positions, duplicate-pending check. Volume/SL-TP checks are
skipped correctly for modify/cancel (not meaningful for those actions) but
still required for close.

**position_sizing.py** — `compute_volume()`: explicit (validated as given,
never silently rounded) or risk_derived (computed from equity × risk% ÷
(SL distance × contract_size), always rounded DOWN to the volume step,
never up). Both paths validated against the symbol's own volume_min/max/step
before a proposal is allowed to exist (§11.1).

**proposal_service.py** — `create_proposal()`: resolves + ownership-checks
the TradingAccount, loads RiskConfiguration, fetches fresh MetaApi
account/symbol/quote/positions data, sizes the order, runs risk_engine,
and — only if it passes — creates the TradeProposal row and calls the
existing `HumanApprovalService.create_and_notify()` for notification
(reusing the platform's HITL notify/expire/Redis-idempotency machinery,
not duplicating it). A request that fails validation never becomes a
stored proposal.

**execution_service.py** — `approve_proposal()`/`reject_proposal()`/
`execute_proposal()`: deliberately NOT wired through the platform's generic
`HumanApprovalService.handle_reply()` (which re-fires the *agent run*,
wrong model for a financial order needing fresh MetaApi revalidation).
Every approval/rejection requires `trading:approve` (checked via the real
`PermissionService`, same one the rest of the platform uses). Duplicate
execution is prevented two ways: an application-level compare-and-swap
`UPDATE ... WHERE status = expected` (only one concurrent caller ever gets
`rowcount == 1`) and the DB's own partial unique index from Phase 1.
Execution re-fetches account/symbol/quote fresh from MetaApi, rejects if
price moved >0.5% from the proposal-time price ("conditions changed
materially, propose again"), re-runs risk_engine against the fresh data,
then places the order and records a TradeExecution row.

**RBAC** — `trading.approve` added to `seed_roles_permissions.py` as a
**distinct** permission (§9.5), deliberately outside the standard
create/read/update/delete/manage action set so it's never bundled into
ordinary "trading" resource access — granted by default only to
platform_owner/owner/admin roles, extendable per-tenant via the existing
`custom_permissions` override.

**Tools** (`internal_tools/trading_execution_tools.py` +
`tool_registrations/trading_execution_tools_registry.py`): `propose_market_order`,
`propose_limit_order`, `propose_stop_order`, `propose_close_position`,
`propose_modify_position`, `propose_cancel_order`, `get_pending_proposals`
— all tagged `tool_category="action"`. None of them can execute anything;
they only ever produce a PENDING_APPROVAL proposal.

**API** (`controllers/trading.py`, mounted via `router_registry.py`):
account linking/listing/unlinking, positions/orders/history, market
quote/candles, proposal listing/detail/approve/reject. Same
`{"success": ..., "data"/"error": ...}` shape and
`Depends(get_current_tenant_id)`/`Depends(get_current_account)` convention
as the rest of the platform.

### Bugs found and fixed by this phase's testing

1. **Risk-% formula ignored FX contract size** (risk_engine.py) — the
   original formula computed `price_diff × volume` as the dollar risk,
   which is only correct if 1.0 volume = 1 unit. For real FX lots (1.0 =
   100,000 units), this **understated risk by 5 orders of magnitude**.
   Fixed by adding `SymbolInfo.contract_size` (populated from MetaApi's
   `contractSize` field) and multiplying it into the formula; falls back
   to a loud warning (not silent) when contract size is unavailable.
   Caught by a functional test that used a realistic contract size and
   got a materially different (correct) answer than the naive formula.
2. **Explicit volumes were silently rounded instead of validated**
   (position_sizing.py) — `_snap_to_step()` was applied unconditionally to
   both explicit and risk-derived volumes. An explicit volume the
   user/agent asked for (e.g. 0.015 lots) would silently become 0.01
   instead of being rejected — changing what was actually ordered without
   telling anyone. Fixed: explicit volumes are now validated as given and
   rejected with a clear reason if misaligned; only risk-derived volumes
   are rounded (always down).
3. **Pre-existing platform bug: `Enum(SomeStrEnum, name=...)` without
   `values_callable` sends the Python member NAME to Postgres, not its
   `.value`** — SQLAlchemy 2.0's default enum binding uses `.name`
   ("PENDING") rather than the lowercase `.value` ("pending") the
   migration-created Postgres enum type actually contains, for any
   `enum.StrEnum` whose member names and values differ in case (which is
   every enum in this codebase using the lowercase-value convention).
   Verified with a real Postgres insert: `INSERT ... VALUES ('PENDING')`
   against a `('pending', 'approved', ...)` enum type raises
   `invalid input value for enum`. This affects the **pre-existing**
   `AgentApprovalRequest.status` column (`agent_approval.py`) — i.e. the
   platform's core HITL approval-creation path — not just the new
   `TradeProposal.status` column this phase added. Both were fixed by
   adding `values_callable=lambda enum_cls: [e.value for e in enum_cls]`
   to the `Enum(...)` column definition. **This was flagged to the user as
   a significant pre-existing-code finding, not something introduced by
   this work** — worth independently confirming in whatever environment
   actually runs `HumanApprovalService.create_and_notify()` in production,
   since if this sandbox's finding holds there too, every approval-request
   creation would currently be failing.

### Verification depth (this phase got the most scrutiny of any phase so far)

- `risk_engine.py` / `position_sizing.py`: pure-function unit tests, ~20
  scenarios total, including the two bugs above and their fixes,
  re-verified after each fix.
- `execution_service.py`: **full integration test against a real local
  Postgres** — real `PermissionService` (seeded actual `permissions`/
  `roles`/`role_permissions`/`tenant_account_joins` rows and confirmed
  both allow and deny paths), real SQLAlchemy models (with the enum fix),
  a hand-built fake MetaApi client (no live network calls). Confirmed:
  non-approver blocked, approver succeeds end-to-end through to a real
  `TradeExecution` row, double-approval blocked (`ProposalStateError`),
  exactly one `EXECUTED` row exists, and — separately — a materially stale
  price blocks execution before `create_order` is ever called (asserted by
  making the fake client's `create_order` raise if reached).
- The CAS pattern and the partial-unique-index backstop were also each
  independently proven with raw SQL against the real schema (first UPDATE
  succeeds, second affects 0 rows; second EXECUTED insert for the same
  proposal raises a unique-constraint violation).
- `proposal_service.py`, `trading_execution_tools.py`, `controllers/trading.py`:
  `py_compile` + `ruff check`/`format` clean; not integration-tested end to
  end in this sandbox (would need a live or thoroughly mocked MetaApi
  provisioning flow) — **recommend a real end-to-end proposal-creation
  test** (chat → propose_market_order → PENDING_APPROVAL → approve →
  EXECUTED) in a real dev environment before this ships.
- Migration re-verified with a full up/down/up round-trip after the
  `volume` nullable change.

---

## Phase 6 — Telegram + WhatsApp approval buttons ✅ (commit pending)

**Goal**: let a human approve/reject a pending `TradeProposal` from Telegram
or WhatsApp with a button tap, not just the REST API — closing the gap the
plan flagged (§Modification Map: "audit found NO inline-keyboard
construction and NO `callback_query` handler exist today" / "audit found no
`type: interactive` message ever sent"). Also adds the missing piece for
market-data/chat access: a Trading Agent is reachable via Telegram/WhatsApp
today with **zero setup** (any tenant admin attaches a `TelegramBot`/
`WhatsAppBot` row the same way as any other agent) — this phase only adds
what's new, the **approval identity** path, which is opt-in and separate.

### New/changed files

- `api/src/models/telegram_bot.py`, `api/src/models/whatsapp_bot.py` —
  added nullable `linked_account_id` (FK → `accounts.id`, `ON DELETE
  SET NULL`) to `TelegramConversation`/`WhatsAppConversation`. Ordinary
  chat never requires this; only approve/reject actions from that channel
  check it.
- `api/migrations/versions/20260811_0001_add_channel_identity_linking.py`
  — idempotent, adds both columns. Verified with a full up/down/up
  round-trip against real local Postgres.
- `api/src/services/trading/channel_linking_service.py` (new) —
  `generate_link_code(tenant_id, account_id) -> code` /
  `resolve_link_code(code) -> (tenant_id, account_id) | None`. 8-hex-char
  code, Redis-backed, 10-minute TTL, single-use (deleted on read).
- `api/src/services/charts/chart_image_renderer.py` — generalized from
  `services/slack/slack_chart_renderer.py` (moved via `git mv`, history
  preserved). Added `render_trading_chart_to_png()` for a
  `TradingChartData`-shaped dict (candles + entry/SL/TP/S-R/trendline
  overlays) → PNG bytes, returns `None` on failure rather than raising.
  `slack_message_handler.py`'s one import site updated.
- `api/src/services/telegram/telegram_polling_service.py` — added
  `handle_link_command()` (`/link <code>`) and `handle_callback_query()`
  (`trade:<proposal_id>:<approve|reject>` callback_data), routing straight
  to `execution_service.approve_proposal`/`reject_proposal` — **not**
  through the platform's generic `HumanApprovalService.handle_reply()`,
  same design decision `execution_service.py` already documented in Phase
  5 (a financial order needs its own MetaApi revalidation, not a re-fired
  agent run).
- `api/src/bot_worker/worker.py::_register_telegram_handlers` — wired the
  two methods above via `CommandHandler("link", ...)` and a new
  `CallbackQueryHandler(...)`, following the exact existing closure
  pattern (fresh DB session + fresh bot row per update).
- `api/src/services/whatsapp/whatsapp_webhook_service.py` — WhatsApp has
  no bot-command system, so `LINK <code>` is a plain inbound text message
  (`_handle_link_command`); an Approve/Reject button tap arrives as
  `interactive.button_reply.id` (`_handle_trade_button_reply`), checked
  **before** the existing generic YES/NO HITL text-reply flow so trade
  taps never get misrouted into it.
- `api/src/services/human_approval_service.py` — added a `"telegram"`
  notification channel (`_send_telegram`: inline `Approve`/`Reject`
  keyboard via `InlineKeyboardMarkup`, callback_data
  `trade:<proposal_id>:<approve|reject>`). Extended `_send_whatsapp_business`
  to send a WhatsApp Cloud API interactive button message when
  `approval.tool_args` carries a `proposal_id` (i.e. a trade proposal);
  every other (non-trading) approval on that channel keeps the original
  plain YES/NO text prompt unchanged.
- `api/src/services/agents/internal_tools/trading_execution_tools.py` —
  new `_resolve_notification_channel(db, conversation_id)`: looks up
  whether the conversation that triggered a `propose_*_order` tool call is
  a `TelegramConversation` or a `WhatsAppConversation` (cloud_api only —
  see limitation below) and, if so, routes the approval notification to
  that channel with the right `{bot_id, chat_id}`/`{bot_id, to_phone}`
  instead of the generic in-chat prompt. `ProposalRequest` gained a
  `channel_config` override field (`proposal_service.py`) to carry this
  through `HumanApprovalService.create_and_notify`.
- `api/src/controllers/trading.py` — `POST /api/v1/trading/channel-link/generate`,
  returns a one-time code for the authenticated account (used by both the
  new web page and directly by API consumers).
- **Web**: `web/lib/api/trading.ts` (new, wired into `lib/api/client.ts`'s
  barrel per the existing per-domain-module convention) +
  `web/app/(dashboard)/trading/page.tsx` (new) — a minimal dashboard page:
  lists proposals by status with Approve/Reject buttons, and a "Link
  Telegram / WhatsApp" modal that generates and displays a code. Sidebar
  entry added in `web/components/layout/Sidebar.tsx`. This was implied but
  not built in Phase 5 (approval only existed via raw REST + Telegram/
  WhatsApp built in this phase) — added now so there's at least one
  authenticated-human-in-the-loop surface that doesn't require a bot.

### Known limitation (documented, not silently swallowed)

WhatsApp **device_link (QR-linked)** bots cannot receive the interactive-
button notification or plain-text HITL fallback either — audited and found
that the platform's existing `whatsapp_web` notification channel
(`HumanApprovalService._send_whatsapp_web` → `WhatsAppWebService.send_text_message`)
is **pre-existing, unrelated-to-this-work dead code for a persistent
connection**: it only has a live client during an active QR-linking
session (`web/...` → `WhatsAppWebService.start_session`), while actual
long-running device-link bots are driven by a completely separate
`WhatsAppDeviceLinkManager` (keyed by `bot_id`, its own neonize client per
thread) that `WhatsAppWebService` never populates. `_resolve_notification_channel`
detects this case and deliberately falls back to the generic in-chat
`"chat"` HITL prompt rather than silently trying to send through a
channel that would fail. **Not fixed here** — unifying
`WhatsAppDeviceLinkManager`/`WhatsAppWebService` is a pre-existing platform
gap outside this phase's scope; flagging it for whoever next touches
WhatsApp device-link notifications.

### Verification depth

Real `python-telegram-bot` isn't installed in this sandbox (only declared
in `pyproject.toml`), so a minimal fake `telegram`/`telegram.ext` module
(just enough surface — `Bot.send_message`/`answer_callback_query`/
`edit_message_text`, `InlineKeyboardButton`, `InlineKeyboardMarkup`) stood
in for it; all business logic under test is the real, unmodified
production code, only the third-party network client is faked. All tests
below ran against a real local Postgres 16 test database + real
`redis-server`, per the methodology established in earlier phases:

- **`_resolve_notification_channel`** — 5/5 cases against real Postgres:
  Telegram conversation → `("telegram", {bot_id, chat_id})`; WhatsApp
  cloud_api conversation → `("whatsapp", {bot_id, to_phone})`; WhatsApp
  device_link conversation → falls back to `("chat", {})`; unknown
  conversation_id → `("chat", {})`; `None` conversation_id → `("chat", {})`
  with no DB query issued.
- **WhatsApp inbound flow** (`WhatsAppWebhookService._handle_link_command`/
  `_handle_trade_button_reply`, real production code, `_send_message` and
  `execution_service` mocked) — 8/8 cases: invalid link code rejected;
  mismatched-tenant link code rejected; successful link persists
  `linked_account_id` in `whatsapp_conversations`; link code is single-use
  (second use rejected); button tap from an unlinked number rejected;
  approve button tap calls `execution_service.approve_proposal` with the
  linked account's identity and replies with the new status; reject button
  tap calls `reject_proposal`; a malformed `button_reply.id` is a no-op.
- **Telegram inbound flow** (`TelegramPollingService.handle_link_command`/
  `handle_callback_query`, real production code) — 10/10 cases: `/link`
  with invalid code, with mismatched tenant, and before the user has ever
  messaged the bot (no `TelegramConversation` row yet — correctly asks the
  user to message the bot first rather than crashing on a missing row);
  successful `/link` persists `linked_account_id`; `callback_query` from
  an unlinked chat/user rejected; approve routes to
  `execution_service.approve_proposal` with the linked identity and edits
  the Telegram message to show the new status; reject routes to
  `reject_proposal`; `ApprovalAuthorizationError` surfaces as a Telegram
  alert (`show_alert=True`) instead of crashing; malformed `callback_data`
  is acknowledged and ignored.
- **`HumanApprovalService._send_telegram`** — verified it builds the
  correct `InlineKeyboardMarkup` (`trade:<id>:approve`/`trade:<id>:reject`
  callback_data) for a trade-proposal approval and returns the right
  `notification_ref`.
- **`HumanApprovalService._send_whatsapp_business`** — verified it sends a
  `type: interactive` button payload with the correct button ids for a
  trade proposal, and confirmed the **generic (non-trading) approval path
  is unchanged** (still `type: text` with the original YES/NO prompt) —
  i.e. this change is additive and doesn't alter existing Slack/WhatsApp
  HITL behavior for non-trading autonomous-agent approvals.
- `chart_image_renderer.py`'s `render_trading_chart_to_png` was already
  verified in the prior session (50-candle synthetic dataset with all
  overlay types → real PNG; malformed input → `None`, no exception).
- **Web**: `tsc --noEmit` and `eslint` clean on all new/changed frontend
  files (`lib/api/trading.ts`, `lib/api/client.ts`, the new page,
  `Sidebar.tsx`) — the one `tsc` error in the full-project run is a
  pre-existing, gitignored, stale `.next/dev/types` artifact referencing a
  Phase-4 test page, unrelated to this change. Loaded `/trading` in a real
  `next dev` + headless Chromium session: page compiles and serves, no
  React/module errors — it correctly redirects into the auth flow because
  no backend/API server is running in this sandbox, identical behavior to
  every other `(dashboard)/*` page under the same conditions. **Not**
  visually verified against a real logged-in session with real proposal
  data — recommend a manual click-through (generate a proposal, approve
  it from the web page, from Telegram, and from WhatsApp separately,
  confirm exactly one `EXECUTED` row and matching `ActivityLog` entries
  for each) in a real dev environment before this ships, same
  recommendation Phase 5 made for the end-to-end proposal flow generally.
- `py_compile` + `ruff check` + `ruff format --check` clean on every
  touched/created Python file — cross-checked against each file's
  pre-Phase-6 (`git show HEAD:...`) lint output file-by-file to confirm
  the **exact same** pre-existing finding count survives unchanged (no new
  issues introduced, no pre-existing issues silently fixed as a
  side-effect): `worker.py` 6=6, `trading_execution_tools.py` 1=1,
  `proposal_service.py` 1=1, `human_approval_service.py` 2=2,
  `whatsapp_webhook_service.py` 3=3, `controllers/trading.py` 1=1,
  `telegram_polling_service.py` 4=4, `models/telegram_bot.py` 1=1,
  `models/whatsapp_bot.py` 1=1.

---

## Phase 7 — MCP Apps / ext-apps investigation 🔍 Investigated, deferred

The plan gated this phase explicitly: "only if the ext-apps spec is
confirmed feasible against the actual installed MCP SDK version at that
time." It isn't — this section documents why, precisely, so a future pass
doesn't have to redo this research.

### What was checked

1. **Installed SDK**: `mcp==1.28.1` (pulled in transitively via
   `fastmcp==3.2.4`, per `api/uv.lock`). Installed it standalone
   (`pip install --no-deps mcp==1.28.1`) into a scratch dir and inspected
   `mcp/types.py` directly rather than trusting docs/memory.
2. **Capability surface**: `ServerCapabilities`/`ClientCapabilities` in
   this SDK version cover `roots`, `sampling`, `elicitation`, `tasks`,
   `prompts`, `resources`, `tools`, `logging`, `completions` — **no
   dedicated "apps" or "ui" capability exists**. "MCP Apps" (the `ui://`
   interactive-resource proposal, sometimes called MCP-UI) is not a
   first-class part of the base protocol this SDK implements; it is a
   community/vendor convention layered on top of the existing generic
   `EmbeddedResource` content-block type (`type: "resource"`, arbitrary
   URI, arbitrary `mimeType` e.g. `text/html`) — which the SDK **does**
   support structurally, since `EmbeddedResource`/`ResourceLink` are
   already part of `ContentBlock` in 1.28.1. So there's no SDK-version
   blocker to constructing such a content block; the blocker is entirely
   on the client-rendering side (next point).
3. **How Synkora actually exposes MCP today**: `api/src/services/agents/mcp_server_host_service.py`
   is a **hand-rolled JSON-RPC 2.0 server**, not built on the `mcp` Python
   SDK's server classes at all (no `import mcp`/`from mcp import ...`
   anywhere in that file). It declares only `"capabilities": {"tools": {}}`
   at `initialize` and its `tools/call` handler for the "chat" tool
   returns only `{"type": "text", "text": ...}` content blocks — no
   `resources` capability declared, no `EmbeddedResource` ever
   constructed. This is the **only** MCP surface Synkora exposes outward
   (confirmed in the original audit) — external clients (Claude Desktop,
   etc.) reach a Synkora agent exclusively through this single "chat"
   tool, never through a richer, per-capability MCP tool set.
4. **Where the value would land**: Synkora's **own** web chat UI does not
   consume MCP protocol messages for its own agents' tool calls at all —
   the Phase 4 klinecharts candlestick chart already reaches the browser
   through the existing, unrelated `message.metadata.charts` mechanism.
   So a `ui://` interactive chart embedded in an MCP tool result would
   only ever be visible to an **external third-party MCP client** chatting
   with the hosted Trading Agent — never to Synkora's own users, who
   already get a strictly better native chart today.

### Why this is deferred, not built

- The actual gating uncertainty isn't the SDK version, it's whether **any**
  MCP client this could plausibly be tested against actually renders a
  `ui://`/HTML `EmbeddedResource` as a sandboxed interactive surface
  rather than inert content. No such client is available in this sandbox
  to verify against, and the proposal itself was still evolving/
  non-finalized as of this session — shipping against an unstable,
  unverifiable external-client behavior is exactly the kind of
  unverified-guess work this whole engagement has deliberately avoided
  (see the verification methodology used in every other phase).
- The benefit is narrow even if it worked: only external MCP clients
  benefit, and only for the Trading Agent's single bundled "chat" tool —
  Synkora's own users already have the richer native chart.
- Building it would mean hand-constructing spec-adjacent JSON inside
  `mcp_server_host_service.py`'s manual JSON-RPC responses with no way to
  confirm it round-trips correctly against a real client, which fails
  this project's own verification bar.

### What a future pass should do instead of starting from scratch

1. Re-check whether `mcp` has shipped an official `apps`/`ui` capability
   (`ServerCapabilities`/`ClientCapabilities` in `mcp/types.py`) — if so,
   the spec has stabilized and this is worth revisiting.
2. Identify a concrete target client that has *confirmed, testable*
   `ui://`/HTML-resource rendering support, and validate against it
   directly rather than guessing at the wire format.
3. If both of the above hold, the additive change is small and isolated:
   add a `resources: {}` capability at `_handle_initialize` (or a new
   capability key once standardized) and, only for the Trading Agent, let
   `_handle_tools_call`'s result optionally include an
   `EmbeddedResource`/`ResourceLink` content block carrying the same
   `TradingChartData` payload the web klinecharts renderer already
   consumes (`web/lib/types/trading.ts`) — same data shape, two render
   targets. The existing `{"type": "text", ...}` content block stays
   first in the list unconditionally, so any client that doesn't
   understand the resource block still gets a working plain-text
   response — this is what "additive, doesn't break the legacy JSON chart
   path" means concretely for this specific integration point.

No code was changed for this phase.

---

## Phase 8 — Production hardening ✅ (commit pending)

**Goal**: rate limiting, retry/error-handling, observability, and deployment
documentation for the trading domain — extending existing platform infra in
each case (RULE 15), plus closing a real gap this pass surfaced: nothing in
the codebase actually provisions the platform-owned broker credentials the
whole domain depends on.

### Rate limiting

Extended `RateLimitMiddleware.ENDPOINT_LIMITS` (`api/src/middleware/rate_limit_middleware.py`)
— no new mechanism, this is the platform's only rate-limiting infra
(Redis sliding-window, path-prefix keyed). Added:
- `/api/v1/trading/channel-link/generate`: 5/min — each call mints a fresh
  one-time code; tight to limit enumeration/abuse.
- `/api/v1/trading/`: 30/min general — everything else (quotes, proposals,
  approve/reject) is authenticated, tenant-scoped, human-paced activity.

The more specific prefix is listed first, since `_get_limit_for_path`
returns on the first `startswith` match — verified directly against the
real middleware class for both prefixes plus a pre-existing route, to
confirm the ordering actually resolves as intended rather than assuming it
from reading the code.

### Retry/backoff on broker calls

`OandaClient`/`MetaApiClient` previously had no retry at all — one failed
request = one failed call. Reused the existing retry primitives from
`src/services/oauth/http_client.py` (`calculate_backoff_delay`,
`RETRYABLE_STATUS_CODES` — same utilities OAuth provider clients already
use) rather than adding a second retry implementation.

**Safety-critical asymmetry, deliberate**: `OandaClient._get` (always GET,
always idempotent) retries fully on timeout/connection failure/429/5xx.
`MetaApiClient._request` retries **GET only** — `create_order`/
`modify_order`/`cancel_order` and account provisioning are POST/DELETE,
and a timeout there doesn't tell us whether MetaApi already placed the
order; blanket-retrying could double-execute a trade. This isn't a
generic idempotency-key situation MetaApi's REST API is known to support,
so the conservative choice is no automatic retry for any state-changing
MetaApi call — callers (`execution_service.py`) already fail closed on a
`BrokerTimeoutError`/`BrokerRequestError` from these paths, which is
correct: an ambiguous execution outcome must surface as a failure requiring
human/operational follow-up, never a silent retry.

### Observability

`ActivityLog` (`ActivityType.TRADING`) already covered the full proposal
lifecycle from Phase 5 (`proposal_created`, `approval_requested`,
`approved`, `rejected`, `execution_started`, `execution_succeeded`,
`execution_failed`) — confirmed this is genuinely the platform's only
event/audit mechanism (no separate metrics-only event bus exists anywhere
in `services/`), so no new infra was needed there. One real gap found:
account-linking (§16/§17, Phase 6) — a security-relevant event, since it
grants a channel identity approval authority — wasn't logged anywhere.
Added `ActivityLog.log_activity(action="trading.channel_linked", ...)` to
both `TelegramPollingService.handle_link_command` and
`WhatsAppWebhookService._handle_link_command`'s success paths.

### Deployment: closed a real "how does this ever work in production" gap

Audited every code path that reads a `PlatformBrokerCredential` row
(`broker_credential_service.py`) against everything that could plausibly
create one, and found **nothing does** — no controller endpoint, no seed
script, nothing. Every other platform-owned secret in this codebase
(SMTP, Stripe, OAuth apps) has a `seed_platform_config.py`-style script;
this one didn't, meaning a fresh deployment following only what existed
before this phase would have market data and execution permanently
broken with a "No active platform OANDA credential configured" error and
no way to fix it short of a manual DB insert.

Added `api/seed_platform_broker_credentials.py`, matching the existing
`seed_trading_agent.py`'s async/argparse style exactly: reads
`OANDA_TOKEN`/`OANDA_ACCOUNT_ID`/`OANDA_ENVIRONMENT`/`METAAPI_TOKEN` from
the environment, creates or (`--update`) overwrites the corresponding
`PlatformBrokerCredential` rows, Fernet-encrypting the token through the
model's existing `.token` property (same pattern as every other encrypted
credential in this codebase — no new encryption code). Added a
`# Trading (MetaApi / OANDA)` block to `api/.env.example` in the same
style as every other integration section, explicitly noting these vars
aren't read directly at runtime — the seed script is the one write path,
consistent with how the rest of the platform's secrets work.

### Verification depth

- **Retry logic** — 5/5 cases against a real `httpx.MockTransport` (no
  network, real request/response cycle through the actual client code):
  OANDA retries on 503 then succeeds (3 attempts); OANDA exhausts retries
  on persistent 503 then raises `BrokerRequestError` (4 attempts, matching
  `_MAX_RETRIES + 1`); OANDA does not retry a 401 (1 attempt, fails fast);
  MetaApi retries a GET on 502 then succeeds (2 attempts); **MetaApi never
  retries a POST, even on a retryable 503 status (1 attempt only)** — this
  last case is the one that actually matters and was verified explicitly,
  not just asserted in a docstring.
- **Rate-limit path resolution** — 5/5 cases against the real
  `RateLimitMiddleware._get_limit_for_path`, confirming both new trading
  prefixes and one pre-existing prefix all resolve to the intended limits.
- **`seed_platform_broker_credentials.py`** — 6/6 cases against real
  Postgres with real Fernet encryption (`ENCRYPTION_KEY`-driven, same
  infra as every other credential in the platform): no tokens in
  environment → nothing created; OANDA token → row created, `_token_enc`
  genuinely encrypted at rest (plaintext token confirmed absent from the
  stored value), decrypts back correctly through `.token`; re-run without
  `--update` leaves the existing row untouched and reports it as skipped;
  re-run with `--update` overwrites it; MetaApi token seeds independently
  with its own default `environment='production'`.
- **`trading.channel_linked` ActivityLog entries** — verified the exact
  `log_activity(...)` call added to both channel-link success paths
  inserts correctly against a real `activity_logs` table (enum binding,
  FK columns, hash-chain-compatible nullable `entry_hash`).
- Full regression pass: every `.py` file touched across all 8 phases
  (45 files) — `py_compile` clean on all of them; `ruff check` finding
  count compared file-by-file against each file's last-committed baseline
  (not just "found N errors" in isolation, since e.g. `models/__init__.py`
  checked outside its package context reports spurious unused-import
  findings) — zero new findings introduced anywhere, all pre-existing
  findings left exactly as they were; `ruff format --check` clean on
  every file this phase touched.

### What's still explicitly NOT done (honest scope boundary)

- No load/soak testing of the Redis sliding-window limiter under real
  concurrent trading-agent traffic — the limits chosen are reasoned
  defaults (matching the platform's existing tiers for comparable
  endpoints), not empirically tuned.
- No Langfuse/LLM-tracing-specific instrumentation was added for trading
  tool calls beyond what `ActivityLog` already captures — the existing
  `LangfuseService` covers LLM-call tracing platform-wide already and
  trading tool calls flow through the same `ADKToolRegistry.execute_tool`
  path as every other tool, so they're already covered by whatever the
  platform does generically; no trading-specific gap was found there.
- `seed_platform_broker_credentials.py` was verified against a real
  Postgres row round-trip, but **not** against a real OANDA/MetaApi
  account — running the actual market-data/execution flow end-to-end
  against live (or demo) broker accounts, per every earlier phase's
  standing recommendation, is still the one thing this sandbox cannot do
  and should happen before this ships.

---

## Overall summary — all 8 phases complete

All 8 phases of the approved plan are done: 6 phases of feature work
(foundation, read-only tools, the Trading Agent, web charts,
approval-gated execution, Telegram/WhatsApp approval) plus one
investigation phase (MCP Apps, deliberately deferred with documented
rationale rather than built on unverifiable assumptions) and one hardening
pass. Every phase followed the same discipline: implement, verify as
deeply as the sandbox allows (real local Postgres/Redis wherever state or
SQL correctness was at stake, isolated logic tests otherwise, never just
"it looks right"), document honestly — including bugs found in this
session's own new code and, twice, in pre-existing platform code — commit,
push. Three significant pre-existing-platform findings were surfaced along
the way and are called out in their respective phase sections above rather
than buried: the `ApprovalStatus`/`Enum` binding bug affecting the core
HITL path (Phase 5), the WhatsApp device-link notification gap (Phase 6),
and the missing platform-broker-credential seeding path (Phase 8).

**Before this ships**, the recurring "not done in this sandbox" items across
every phase converge on one recommendation: a real end-to-end run against
live/demo OANDA and MetaApi accounts — link a demo MetaApi account, get a
quote, get a recommendation, propose a trade, approve it from the web
dashboard, from Telegram, and from WhatsApp separately, confirm exactly
one execution lands each time, and run the full `pytest` suite plus
`alembic upgrade head` in an environment that isn't missing `xmlsec`/a
working `cryptography` install. Everything that could be verified without
those has been.
