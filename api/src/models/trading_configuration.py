"""
Risk Configuration Model

Per-trading-account risk settings used by the risk engine and position
sizing service (see src/services/trading/risk_engine.py and
position_sizing.py). Stop-loss is always required for new positions — that
is enforced in code (risk_engine.py), not via a configurable flag here, so
it can never be silently disabled through configuration.
"""

from sqlalchemy import Column, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from src.models.base import BaseModel, TenantMixin


class RiskConfiguration(BaseModel, TenantMixin):
    """
    Risk configuration for a single TradingAccount.

    Attributes:
        trading_account_id: The TradingAccount this configuration governs
        position_sizing_method: 'explicit' (agent/user supplies volume) or
            'risk_derived' (server computes volume from equity risk %)
        max_risk_pct_per_trade: Max % of account equity risked per trade,
            used when position_sizing_method == 'risk_derived'
        max_position_size: Optional hard cap on volume/lot size, any method
        max_open_positions: Optional cap on concurrent open positions
        max_daily_loss_pct: Optional daily loss circuit breaker (% of equity)
    """

    __tablename__ = "risk_configurations"

    trading_account_id = Column(
        UUID(as_uuid=True),
        ForeignKey("trading_accounts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
        comment="TradingAccount this configuration governs",
    )

    position_sizing_method = Column(String(20), nullable=False, default="explicit", comment="explicit | risk_derived")

    max_risk_pct_per_trade = Column(
        Numeric(5, 2), nullable=True, comment="Max % of equity risked per trade (risk_derived sizing)"
    )

    max_position_size = Column(Numeric(15, 2), nullable=True, comment="Hard cap on volume/lot size")

    max_open_positions = Column(Integer, nullable=True, comment="Cap on concurrent open positions")

    max_daily_loss_pct = Column(Numeric(5, 2), nullable=True, comment="Daily loss circuit breaker, % of equity")

    trading_account = relationship("TradingAccount", back_populates="risk_configuration")

    def __repr__(self) -> str:
        return (
            f"<RiskConfiguration(trading_account_id={self.trading_account_id}, method='{self.position_sizing_method}')>"
        )
