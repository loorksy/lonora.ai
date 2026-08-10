"""
Trading Account Model

Represents one user's linked MetaApi (MT4/MT5) trading account within a
tenant. This is strictly per-user execution state — market data/analysis
never requires one of these (see MarketDataService, which is OANDA-backed
via the single PlatformBrokerCredential instead).

The MT investor/master password is Fernet-encrypted at rest using the same
private-column + property pattern as MCPServer.auth_config. The MT login
and server name are not treated as secrets (comparable to a username),
matching how MetaApi itself treats them.
"""

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from src.models.base import BaseModel, TenantMixin


class TradingAccount(BaseModel, TenantMixin):
    """
    A user's linked MetaApi trading account.

    Attributes:
        account_id: Owning Account (the user who linked this account)
        broker: Execution broker identifier, always 'metaapi' today
        metaapi_account_id: MetaApi's own account resource id (assigned once
            provisioning succeeds; null while a link request is pending)
        mt_login: MT4/MT5 login number
        mt_server: MT4/MT5 broker server name
        mt_password: Decrypted MT investor/master password (property backed
            by _mt_password_enc) — needed for (re)provisioning with MetaApi
        label: User-friendly display name
        status: pending | connected | error | disabled
        connected_at: When MetaApi confirmed the account is deployed/connected
        last_synced_at: Last time account info/positions were refreshed
    """

    __tablename__ = "trading_accounts"

    account_id = Column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Owning account (user who linked this MetaApi account)",
    )

    broker = Column(String(20), nullable=False, default="metaapi", comment="Execution broker identifier")

    metaapi_account_id = Column(
        String(100), nullable=True, index=True, comment="MetaApi account resource id (null until provisioned)"
    )

    mt_login = Column(String(100), nullable=False, comment="MT4/MT5 login number")

    mt_server = Column(String(255), nullable=False, comment="MT4/MT5 broker server name")

    # Backing column: stores Fernet-encrypted password ("enc:<token>") or NULL.
    # Access only via the .mt_password property — never read _mt_password_enc directly.
    _mt_password_enc = Column(
        "mt_password", Text, nullable=True, comment="MT investor/master password (Fernet-encrypted)"
    )

    label = Column(String(255), nullable=True, comment="User-friendly display name")

    status = Column(
        String(20), nullable=False, default="pending", index=True, comment="pending|connected|error|disabled"
    )

    connected_at = Column(
        DateTime(timezone=True), nullable=True, comment="When MetaApi confirmed deployment/connection"
    )

    last_synced_at = Column(DateTime(timezone=True), nullable=True, comment="Last account info/positions refresh")

    risk_configuration = relationship(
        "RiskConfiguration", back_populates="trading_account", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "account_id", "mt_login", "mt_server", name="uq_trading_account_link"),
    )

    @property
    def mt_password(self) -> str | None:
        """Return the decrypted MT investor/master password."""
        raw = self._mt_password_enc
        if not raw:
            return None
        if raw.startswith("enc:"):
            from src.services.agents.security import decrypt_value  # noqa: PLC0415

            return decrypt_value(raw[4:])
        return raw

    @mt_password.setter
    def mt_password(self, value: str | None) -> None:
        if value is None:
            self._mt_password_enc = None
            return
        from src.services.agents.security import encrypt_value  # noqa: PLC0415

        self._mt_password_enc = f"enc:{encrypt_value(value)}"

    def to_dict(self, exclude: set[str] | None = None) -> dict:
        """Convert to dictionary, always excluding the MT password."""
        exclude = exclude or set()
        exclude.add("mt_password")
        return super().to_dict(exclude=exclude)

    def __repr__(self) -> str:
        return f"<TradingAccount(id={self.id}, mt_login='{self.mt_login}', status='{self.status}')>"
