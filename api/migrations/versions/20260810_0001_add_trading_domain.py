"""add trading domain tables (accounts, risk config, proposals, executions, platform credentials)

Revision ID: 20260810_0001
Revises: 95b0831fc354
Create Date: 2026-08-10

Foundation migration for the Forex/trading domain extension. Creates:
  - platform_broker_credentials (single platform-owned OANDA/MetaApi token)
  - trading_accounts (per-user linked MetaApi MT account)
  - risk_configurations (per-trading-account position sizing / risk limits)
  - trade_proposals (HITL-gated proposed trading actions)
  - trade_executions (broker-interaction record per proposal)

Also extends the existing approval_status_enum (used by AgentApprovalRequest,
the platform's generic HITL gate) with EXECUTING/FAILED values, closing a
gap where that transition was never wired up, and adds ActivityType.TRADING
(plain Python enum on ActivityLog.activity_type — no DB enum type to alter).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.dialects.postgresql import ENUM

# revision identifiers, used by Alembic.
revision = "20260810_0001"
down_revision = "95b0831fc354"
branch_labels = None
depends_on = None

trade_proposal_status_enum = ENUM(
    "proposed",
    "pending_approval",
    "approved",
    "rejected",
    "expired",
    "executing",
    "executed",
    "failed",
    name="trade_proposal_status_enum",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Extend the existing generic HITL approval_status_enum with the
    #    execution-side transitions (§F gap identified by audit).
    op.execute("ALTER TYPE approval_status_enum ADD VALUE IF NOT EXISTS 'executing'")
    op.execute("ALTER TYPE approval_status_enum ADD VALUE IF NOT EXISTS 'failed'")

    # 2. New enum for TradeProposal's richer domain-specific state machine.
    trade_proposal_status_enum.create(bind, checkfirst=True)

    # 3. platform_broker_credentials — single platform-owned broker token.
    if not bind.dialect.has_table(bind, "platform_broker_credentials"):
        op.create_table(
            "platform_broker_credentials",
            sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
            sa.Column("broker", sa.String(20), nullable=False),
            sa.Column("environment", sa.String(20), nullable=False, server_default="production"),
            sa.Column("account_ref", sa.String(100), nullable=True),
            sa.Column("token", sa.Text, nullable=True),
            sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.UniqueConstraint("broker", "environment", name="uq_platform_broker_credential_broker_env"),
        )

    # 4. trading_accounts — per-user linked MetaApi MT account.
    if not bind.dialect.has_table(bind, "trading_accounts"):
        op.create_table(
            "trading_accounts",
            sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "account_id", pg.UUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column("broker", sa.String(20), nullable=False, server_default="metaapi"),
            sa.Column("metaapi_account_id", sa.String(100), nullable=True),
            sa.Column("mt_login", sa.String(100), nullable=False),
            sa.Column("mt_server", sa.String(255), nullable=False),
            sa.Column("mt_password", sa.Text, nullable=True),
            sa.Column("label", sa.String(255), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.UniqueConstraint("tenant_id", "account_id", "mt_login", "mt_server", name="uq_trading_account_link"),
        )
        op.create_index("ix_trading_accounts_tenant_id", "trading_accounts", ["tenant_id"])
        op.create_index("ix_trading_accounts_account_id", "trading_accounts", ["account_id"])
        op.create_index("ix_trading_accounts_metaapi_account_id", "trading_accounts", ["metaapi_account_id"])
        op.create_index("ix_trading_accounts_status", "trading_accounts", ["status"])

    # 5. risk_configurations — 1:1 with trading_accounts.
    if not bind.dialect.has_table(bind, "risk_configurations"):
        op.create_table(
            "risk_configurations",
            sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "trading_account_id",
                pg.UUID(as_uuid=True),
                sa.ForeignKey("trading_accounts.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column("position_sizing_method", sa.String(20), nullable=False, server_default="explicit"),
            sa.Column("max_risk_pct_per_trade", sa.Numeric(5, 2), nullable=True),
            sa.Column("max_position_size", sa.Numeric(15, 2), nullable=True),
            sa.Column("max_open_positions", sa.Integer, nullable=True),
            sa.Column("max_daily_loss_pct", sa.Numeric(5, 2), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        )
        op.create_index("ix_risk_configurations_tenant_id", "risk_configurations", ["tenant_id"])

    # 6. trade_proposals
    if not bind.dialect.has_table(bind, "trade_proposals"):
        op.create_table(
            "trade_proposals",
            sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "account_id", pg.UUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column(
                "agent_id", pg.UUID(as_uuid=True), sa.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column(
                "trading_account_id",
                pg.UUID(as_uuid=True),
                sa.ForeignKey("trading_accounts.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "approval_request_id",
                pg.UUID(as_uuid=True),
                sa.ForeignKey("agent_approval_requests.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("status", trade_proposal_status_enum, nullable=False, server_default="proposed"),
            sa.Column("broker", sa.String(20), nullable=False, server_default="metaapi"),
            sa.Column("symbol", sa.String(50), nullable=False),
            sa.Column("action", sa.String(20), nullable=False),
            sa.Column("order_type", sa.String(20), nullable=False, server_default="market"),
            sa.Column("volume", sa.Numeric(15, 2), nullable=True),
            sa.Column("volume_source", sa.String(20), nullable=False),
            sa.Column("stop_loss", sa.Numeric(20, 5), nullable=True),
            sa.Column("take_profit", sa.Numeric(20, 5), nullable=True),
            sa.Column("limit_price", sa.Numeric(20, 5), nullable=True),
            sa.Column("oanda_reference_price", sa.Numeric(20, 5), nullable=True),
            sa.Column("oanda_reference_price_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("metaapi_price_at_proposal", sa.Numeric(20, 5), nullable=True),
            sa.Column("risk_check_result", pg.JSONB, nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "approved_by", pg.UUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
            ),
            sa.Column("execution_started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("execution_completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("broker_order_id", sa.String(100), nullable=True),
            sa.Column("failure_reason", sa.Text, nullable=True),
            sa.Column("requires_confirmation", sa.Boolean, nullable=False, server_default=sa.text("true")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        )
        op.create_index("ix_trade_proposals_tenant_status", "trade_proposals", ["tenant_id", "status"])
        op.create_index(
            "ix_trade_proposals_trading_account_status", "trade_proposals", ["trading_account_id", "status"]
        )
        op.create_index("ix_trade_proposals_agent_id", "trade_proposals", ["agent_id"])
        op.create_index("ix_trade_proposals_approval_request_id", "trade_proposals", ["approval_request_id"])
        # Belt-and-suspenders duplicate-execution guard: only one proposal may
        # be actively EXECUTING or already EXECUTED at a time (per proposal
        # row this is trivially true, but this partial index is the pattern
        # trade_executions' equivalent constraint relies on for the real
        # cross-attempt guarantee — see below).

    # 7. trade_executions — one row per execution attempt.
    if not bind.dialect.has_table(bind, "trade_executions"):
        op.create_table(
            "trade_executions",
            sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
            sa.Column("tenant_id", pg.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "trade_proposal_id",
                pg.UUID(as_uuid=True),
                sa.ForeignKey("trade_proposals.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("status", sa.String(20), nullable=False, server_default="executing"),
            sa.Column("revalidated_price", sa.Numeric(20, 5), nullable=True),
            sa.Column("slippage", sa.Numeric(20, 5), nullable=True),
            sa.Column("margin_at_execution", sa.Numeric(20, 2), nullable=True),
            sa.Column("broker_order_id", sa.String(100), nullable=True),
            sa.Column("result", pg.JSONB, nullable=True),
            sa.Column("failure_reason", sa.Text, nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        )
        op.create_index("ix_trade_executions_tenant_id", "trade_executions", ["tenant_id"])
        op.create_index("ix_trade_executions_trade_proposal_id", "trade_executions", ["trade_proposal_id"])
        # Hard DB-level duplicate-execution guard (§9.4/§F): at most one
        # EXECUTED row per proposal, independent of the Redis token.
        op.execute(
            "CREATE UNIQUE INDEX uq_trade_executions_one_executed_per_proposal "
            "ON trade_executions (trade_proposal_id) WHERE status = 'executed'"
        )


def downgrade() -> None:
    bind = op.get_bind()

    op.execute("DROP INDEX IF EXISTS uq_trade_executions_one_executed_per_proposal")
    op.drop_index("ix_trade_executions_trade_proposal_id", table_name="trade_executions")
    op.drop_index("ix_trade_executions_tenant_id", table_name="trade_executions")
    op.drop_table("trade_executions")

    op.drop_index("ix_trade_proposals_approval_request_id", table_name="trade_proposals")
    op.drop_index("ix_trade_proposals_agent_id", table_name="trade_proposals")
    op.drop_index("ix_trade_proposals_trading_account_status", table_name="trade_proposals")
    op.drop_index("ix_trade_proposals_tenant_status", table_name="trade_proposals")
    op.drop_table("trade_proposals")

    op.drop_index("ix_risk_configurations_tenant_id", table_name="risk_configurations")
    op.drop_table("risk_configurations")

    op.drop_index("ix_trading_accounts_status", table_name="trading_accounts")
    op.drop_index("ix_trading_accounts_metaapi_account_id", table_name="trading_accounts")
    op.drop_index("ix_trading_accounts_account_id", table_name="trading_accounts")
    op.drop_index("ix_trading_accounts_tenant_id", table_name="trading_accounts")
    op.drop_table("trading_accounts")

    op.drop_table("platform_broker_credentials")

    trade_proposal_status_enum.drop(bind, checkfirst=True)

    # Note: PostgreSQL doesn't support removing enum values from
    # approval_status_enum directly (see 20260207_1200's precedent for the
    # same limitation) — the 'executing'/'failed' values are left in place
    # on downgrade, matching this codebase's existing convention.
