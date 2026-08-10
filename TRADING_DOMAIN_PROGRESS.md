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
