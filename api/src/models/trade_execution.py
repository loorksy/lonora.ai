"""
Trade Execution Model

The broker-interaction record for a TradeProposal's execution attempt.
Kept separate from TradeProposal so the proposal stays a clean "what was
requested / approved" record while this captures "what actually happened
when we talked to MetaApi" — including the price/margin revalidation done
immediately before sending the order (see src/services/trading/execution_service.py).

A unique partial index (see the accompanying migration) allows only one
EXECUTED row per trade_proposal_id — the hard DB-level backstop against
duplicate execution, alongside the Redis one-time approval token already
used by the platform's HITL system.
"""

from sqlalchemy import Column, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from src.models.base import BaseModel, TenantMixin


class TradeExecution(BaseModel, TenantMixin):
    """
    One execution attempt for a TradeProposal.

    Attributes:
        trade_proposal_id: The proposal being executed
        status: executing | executed | failed
        revalidated_price: Price re-fetched from MetaApi immediately before
            sending the order (§10 — never the OANDA reference price)
        slippage: Difference between proposal reference and revalidated price
        margin_at_execution: Available margin at revalidation time
        broker_order_id: MetaApi order/position id on success
        result: Sanitized broker response metadata (never credentials)
        failure_reason: Human-readable reason on failure
        started_at / completed_at: Execution timing
    """

    __tablename__ = "trade_executions"

    trade_proposal_id = Column(
        UUID(as_uuid=True), ForeignKey("trade_proposals.id", ondelete="CASCADE"), nullable=False, index=True
    )

    status = Column(String(20), nullable=False, default="executing", index=True, comment="executing|executed|failed")

    revalidated_price = Column(Numeric(20, 5), nullable=True)
    slippage = Column(Numeric(20, 5), nullable=True)
    margin_at_execution = Column(Numeric(20, 2), nullable=True)

    broker_order_id = Column(String(100), nullable=True)
    result = Column(JSONB, nullable=True, comment="Sanitized broker response metadata")
    failure_reason = Column(Text, nullable=True)

    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    trade_proposal = relationship("TradeProposal", back_populates="executions")

    def __repr__(self) -> str:
        return f"<TradeExecution(id={self.id}, trade_proposal_id={self.trade_proposal_id}, status='{self.status}')>"
