"""
Broker credential lookup + adapter construction.

The only place that reads PlatformBrokerCredential/TradingAccount secrets
and turns them into a ready-to-use broker adapter. Callers (trading tools,
domain services) never touch encrypted columns directly — this keeps
credential handling in one auditable place, per the platform's existing
encrypted-credential conventions (src/services/agents/security.py).
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.platform_broker_credential import PlatformBrokerCredential
from src.models.trading_account import TradingAccount
from src.services.trading.brokers.base import BrokerAuthError
from src.services.trading.brokers.metaapi_client import MetaApiClient
from src.services.trading.brokers.oanda_client import OandaClient

logger = logging.getLogger(__name__)


async def get_oanda_client(db: AsyncSession, environment: str = "practice") -> OandaClient:
    """Build an OandaClient from the single platform-owned OANDA credential.

    Raises BrokerAuthError if no active credential is configured for the
    requested environment — callers must surface this as an actionable
    error, never fall back to fabricated data (§7/§13 of the trading spec).
    """
    result = await db.execute(
        select(PlatformBrokerCredential).where(
            PlatformBrokerCredential.broker == "oanda",
            PlatformBrokerCredential.environment == environment,
            PlatformBrokerCredential.is_active.is_(True),
        )
    )
    credential = result.scalar_one_or_none()
    if credential is None or not credential.token:
        raise BrokerAuthError(
            f"No active platform OANDA credential configured for environment '{environment}'. "
            "An admin must configure one before market data is available."
        )
    if not credential.account_ref:
        raise BrokerAuthError("Platform OANDA credential is missing its account_ref (OANDA account id).")
    return OandaClient(token=credential.token, environment=environment, account_id=credential.account_ref)


async def get_metaapi_client(db: AsyncSession) -> MetaApiClient:
    """Build a MetaApiClient from the single platform-owned MetaApi token.

    Unlike OANDA, MetaApi has no separate practice/live split at the
    platform-credential level — each linked TradingAccount's own MT login
    determines whether it trades a demo or live MT server.
    """
    result = await db.execute(
        select(PlatformBrokerCredential).where(
            PlatformBrokerCredential.broker == "metaapi",
            PlatformBrokerCredential.is_active.is_(True),
        )
    )
    credential = result.scalar_one_or_none()
    if credential is None or not credential.token:
        raise BrokerAuthError(
            "No active platform MetaApi credential configured. An admin must configure one before "
            "trading accounts can be linked or executed against."
        )
    return MetaApiClient(token=credential.token)


async def get_trading_account_or_raise(db: AsyncSession, tenant_id: str, trading_account_id: str) -> TradingAccount:
    """Load a TradingAccount, enforcing tenant isolation explicitly (no platform-wide auto-filter exists)."""
    result = await db.execute(
        select(TradingAccount).where(
            TradingAccount.id == trading_account_id,
            TradingAccount.tenant_id == tenant_id,
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise ValueError(f"Trading account {trading_account_id} not found for this tenant")
    return account
