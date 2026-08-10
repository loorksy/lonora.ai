"""
Platform Broker Credential Model

Stores the single platform-owned credential used to talk to a market-data /
execution broker on behalf of every tenant (e.g. one OANDA account serving
quotes/candles to the whole platform, or the one org-level MetaApi
provisioning/client-API token used to link and operate every user's MT
account). This is deliberately NOT per-tenant — per-user broker state lives
on TradingAccount instead.

The token is Fernet-encrypted at rest using the same private-column +
property pattern as MCPServer.auth_config (see src/services/agents/security.py).
"""

from sqlalchemy import Boolean, Column, String, Text, UniqueConstraint

from src.models.base import BaseModel


class PlatformBrokerCredential(BaseModel):
    """
    Single platform-owned broker credential (e.g. OANDA API token, MetaApi
    provisioning/client-API token). Not tenant-scoped: one row per
    (broker, environment) serves every tenant on the platform.

    Attributes:
        broker: Broker identifier ('oanda' | 'metaapi')
        environment: Broker environment ('practice' | 'live' for OANDA;
            'production' for MetaApi)
        token: Decrypted broker API token (property backed by _token_enc)
        is_active: Whether this credential is currently in use
    """

    __tablename__ = "platform_broker_credentials"

    broker = Column(String(20), nullable=False, index=True, comment="Broker identifier: oanda | metaapi")

    environment = Column(
        String(20), nullable=False, default="production", comment="Broker environment: practice | live | production"
    )

    account_ref = Column(
        String(100),
        nullable=True,
        comment="Non-secret account identifier the token authenticates as (e.g. OANDA account id). Not encrypted.",
    )

    # Backing column: stores Fernet-encrypted token ("enc:<token>") or NULL.
    # Access only via the .token property — never read _token_enc directly.
    _token_enc = Column("token", Text, nullable=True, comment="Broker API token (Fernet-encrypted)")

    is_active = Column(Boolean, nullable=False, default=True, comment="Whether this credential is currently in use")

    __table_args__ = (UniqueConstraint("broker", "environment", name="uq_platform_broker_credential_broker_env"),)

    @property
    def token(self) -> str | None:
        """Return the decrypted broker API token."""
        raw = self._token_enc
        if not raw:
            return None
        if raw.startswith("enc:"):
            from src.services.agents.security import decrypt_value  # noqa: PLC0415

            return decrypt_value(raw[4:])
        return raw

    @token.setter
    def token(self, value: str | None) -> None:
        if value is None:
            self._token_enc = None
            return
        from src.services.agents.security import encrypt_value  # noqa: PLC0415

        self._token_enc = f"enc:{encrypt_value(value)}"

    def to_dict(self, exclude: set[str] | None = None) -> dict:
        """Convert to dictionary, always excluding the token."""
        exclude = exclude or set()
        exclude.add("token")
        return super().to_dict(exclude=exclude)

    def __repr__(self) -> str:
        return f"<PlatformBrokerCredential(broker='{self.broker}', environment='{self.environment}')>"
