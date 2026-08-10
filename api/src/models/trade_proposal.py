"""
Trade Proposal Model

A trade proposal is the durable record of a requested real-money action
(new position, modify, close, cancel) from the moment the Trading Agent's
propose_* tool creates it through human approval and execution. It is
deliberately a richer, domain-specific record than the generic
AgentApprovalRequest (which only carries a JSON blob of tool args) so that
price/volume/SL/TP fields are typed and queryable for risk checks, audit,
and the execution-revalidation step.

A TradeProposal always has a 1:1 AgentApprovalRequest once it reaches
PENDING_APPROVAL — the platform's existing HITL notify/expire/idempotency
machinery (see src/services/human_approval_service.py) is reused rather than
duplicated; this model only adds the trading-specific fields on top.

Lifecycle (see src/services/trading/proposal_service.py and execution_service.py):

    PROPOSED -> PENDING_APPROVAL -> APPROVED -> EXECUTING -> EXECUTED
                               |-> REJECTED        |-> FAILED
                               |-> EXPIRED
"""

import enum

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from src.models.base import BaseModel, TenantMixin


class TradeProposalStatus(enum.StrEnum):
    PROPOSED = "proposed"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED = "failed"


class TradeProposal(BaseModel, TenantMixin):
    """
    A proposed real-money trading action awaiting/undergoing human approval
    and execution.

    Attributes:
        account_id: The account that proposed this (owner of the conversation/
            agent run — usually the same user as trading_account.account_id)
        agent_id: The agent that generated the proposal
        trading_account_id: The TradingAccount execution will target
        approval_request_id: Linked AgentApprovalRequest once notification
            has been sent (null while still PROPOSED, before risk checks pass)
        broker: Execution broker, always 'metaapi' today
        symbol: Broker-native symbol (MetaApi naming, e.g. 'EURUSD')
        action: buy | sell | close | modify | cancel
        order_type: market | limit | stop
        volume: Order volume/lot size
        volume_source: 'explicit' | 'risk_derived' — which method produced
            `volume` (§11.1 — must always be recorded, never implicit)
        stop_loss / take_profit: Required for new positions (enforced by
            risk_engine.py, not by a nullable-column default here)
        limit_price: Reference price for limit/stop orders
        oanda_reference_price / oanda_reference_price_at: The OANDA price
            that justified the recommendation, captured at proposal time
        metaapi_price_at_proposal: MetaApi-side price at proposal time, if
            available — NEVER the price execution is allowed to act on
            (execution always re-fetches fresh from MetaApi, see §10)
        expires_at: Proposal must be approved before this or it expires
        approved_at / approved_by: Who approved it (must hold trading:approve)
        execution_started_at / execution_completed_at: Execution timing
        broker_order_id: MetaApi order/position id once executed
        failure_reason: Human-readable reason if status == FAILED/REJECTED
    """

    __tablename__ = "trade_proposals"

    account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    trading_account_id = Column(
        UUID(as_uuid=True), ForeignKey("trading_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    approval_request_id = Column(
        UUID(as_uuid=True), ForeignKey("agent_approval_requests.id", ondelete="SET NULL"), nullable=True, index=True
    )

    status = Column(
        SQLEnum(TradeProposalStatus, name="trade_proposal_status_enum"),
        nullable=False,
        default=TradeProposalStatus.PROPOSED,
        index=True,
    )

    broker = Column(String(20), nullable=False, default="metaapi")
    symbol = Column(String(50), nullable=False, comment="Broker-native symbol (MetaApi naming)")
    action = Column(String(20), nullable=False, comment="buy | sell | close | modify | cancel")
    order_type = Column(String(20), nullable=False, default="market", comment="market | limit | stop")

    volume = Column(Numeric(15, 2), nullable=False)
    volume_source = Column(String(20), nullable=False, comment="explicit | risk_derived")

    stop_loss = Column(Numeric(20, 5), nullable=True)
    take_profit = Column(Numeric(20, 5), nullable=True)
    limit_price = Column(Numeric(20, 5), nullable=True, comment="Reference price for limit/stop orders")

    oanda_reference_price = Column(Numeric(20, 5), nullable=True)
    oanda_reference_price_at = Column(DateTime(timezone=True), nullable=True)
    metaapi_price_at_proposal = Column(Numeric(20, 5), nullable=True)

    risk_check_result = Column(JSONB, nullable=True, comment="Risk engine output at proposal creation")

    expires_at = Column(DateTime(timezone=True), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    approved_by = Column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True)

    execution_started_at = Column(DateTime(timezone=True), nullable=True)
    execution_completed_at = Column(DateTime(timezone=True), nullable=True)
    broker_order_id = Column(String(100), nullable=True)
    failure_reason = Column(Text, nullable=True)

    requires_confirmation = Column(
        Boolean, nullable=False, default=True, comment="Always true for real-money actions — no bypass path"
    )

    trading_account = relationship("TradingAccount")
    executions = relationship("TradeExecution", back_populates="trade_proposal", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<TradeProposal(id={self.id}, symbol='{self.symbol}', action='{self.action}', status='{self.status}')>"
