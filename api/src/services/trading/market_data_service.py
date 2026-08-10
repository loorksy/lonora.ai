"""
Source-aware trading data service.

The single place agent tools and API controllers go through to read market
data (OANDA, platform-level) or account state (MetaApi, per-user linked
account). Every response is tagged with an explicit `source` field and never
fabricates data — broker errors propagate as BrokerError subclasses, which
callers turn into actionable tool/API errors rather than silently returning
stale or invented numbers (§7/§13 of the trading domain spec).

This is also the tenant-isolation choke point for MetaApi-backed reads:
every method that takes a `trading_account_id` verifies it belongs to the
calling tenant (and, by default, the calling account) before ever touching
MetaApi.
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.services.trading.broker_credential_service import (
    get_metaapi_client,
    get_oanda_client,
    get_trading_account_or_raise,
)
from src.services.trading.brokers.base import BrokerRequestError, OrderSide, OrderType

logger = logging.getLogger(__name__)


async def _resolve_trading_account(
    db: AsyncSession, tenant_id: str, trading_account_id: str, requesting_account_id: str | None
):
    account = await get_trading_account_or_raise(db, tenant_id, trading_account_id)
    if requesting_account_id and str(account.account_id) != str(requesting_account_id):
        raise BrokerRequestError("This trading account does not belong to the requesting user.")
    if not account.metaapi_account_id:
        raise BrokerRequestError(
            f"Trading account {trading_account_id} is not fully connected yet (no MetaApi account id)."
        )
    return account


async def get_quote(
    db: AsyncSession,
    tenant_id: str,
    source: str,
    symbol: str,
    trading_account_id: str | None = None,
    requesting_account_id: str | None = None,
    oanda_environment: str = "practice",
) -> dict[str, Any]:
    if source == "oanda":
        client = await get_oanda_client(db, environment=oanda_environment)
        quote = await client.get_quote(symbol)
    elif source == "metaapi":
        if not trading_account_id:
            raise BrokerRequestError("trading_account_id is required for source='metaapi'")
        account = await _resolve_trading_account(db, tenant_id, trading_account_id, requesting_account_id)
        client = await get_metaapi_client(db)
        quote = await client.get_quote(account.metaapi_account_id, symbol)
    else:
        raise ValueError(f"Unsupported source '{source}' (expected 'oanda' or 'metaapi')")

    return {
        "source": quote.source,
        "symbol": quote.symbol,
        "bid": str(quote.bid),
        "ask": str(quote.ask),
        "spread": str(quote.spread),
        "timestamp": quote.timestamp.isoformat(),
        "tradeable": quote.tradeable,
    }


async def get_candles(
    db: AsyncSession,
    tenant_id: str,
    source: str,
    symbol: str,
    timeframe: str,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int | None = None,
    trading_account_id: str | None = None,
    requesting_account_id: str | None = None,
    oanda_environment: str = "practice",
) -> dict[str, Any]:
    if source == "oanda":
        client = await get_oanda_client(db, environment=oanda_environment)
        series = await client.get_candles(symbol, timeframe, start=start, end=end, limit=limit)
    elif source == "metaapi":
        if not trading_account_id:
            raise BrokerRequestError("trading_account_id is required for source='metaapi'")
        account = await _resolve_trading_account(db, tenant_id, trading_account_id, requesting_account_id)
        client = await get_metaapi_client(db)
        series = await client.get_candles(
            account.metaapi_account_id, symbol, timeframe, start=start, end=end, limit=limit
        )
    else:
        raise ValueError(f"Unsupported source '{source}' (expected 'oanda' or 'metaapi')")

    return {
        "source": series.source,
        "symbol": series.symbol,
        "timeframe": series.timeframe,
        "candles": [
            {
                "time": c.time.isoformat(),
                "open": str(c.open),
                "high": str(c.high),
                "low": str(c.low),
                "close": str(c.close),
                "volume": str(c.volume) if c.volume is not None else None,
            }
            for c in series.candles
        ],
        "count": len(series.candles),
    }


async def get_instruments(
    db: AsyncSession,
    tenant_id: str,
    source: str,
    trading_account_id: str | None = None,
    requesting_account_id: str | None = None,
    oanda_environment: str = "practice",
) -> dict[str, Any]:
    if source == "oanda":
        client = await get_oanda_client(db, environment=oanda_environment)
        instruments = await client.get_instruments()
        symbols = [i.symbol for i in instruments]
    elif source == "metaapi":
        if not trading_account_id:
            raise BrokerRequestError("trading_account_id is required for source='metaapi'")
        account = await _resolve_trading_account(db, tenant_id, trading_account_id, requesting_account_id)
        client = await get_metaapi_client(db)
        symbols = await client.get_symbols(account.metaapi_account_id)
    else:
        raise ValueError(f"Unsupported source '{source}' (expected 'oanda' or 'metaapi')")

    return {"source": source, "symbols": symbols, "count": len(symbols)}


async def get_symbol_information(
    db: AsyncSession,
    tenant_id: str,
    source: str,
    symbol: str,
    trading_account_id: str | None = None,
    requesting_account_id: str | None = None,
    oanda_environment: str = "practice",
) -> dict[str, Any]:
    if source == "oanda":
        client = await get_oanda_client(db, environment=oanda_environment)
        instruments = await client.get_instruments()
        match = next((i for i in instruments if i.symbol == symbol), None)
        if match is None:
            raise BrokerRequestError(f"OANDA does not list instrument '{symbol}'")
        info = match
    elif source == "metaapi":
        if not trading_account_id:
            raise BrokerRequestError("trading_account_id is required for source='metaapi'")
        account = await _resolve_trading_account(db, tenant_id, trading_account_id, requesting_account_id)
        client = await get_metaapi_client(db)
        info = await client.get_symbol_information(account.metaapi_account_id, symbol)
    else:
        raise ValueError(f"Unsupported source '{source}' (expected 'oanda' or 'metaapi')")

    return {
        "source": info.source,
        "symbol": info.symbol,
        "description": info.description,
        "volume_min": str(info.volume_min) if info.volume_min is not None else None,
        "volume_max": str(info.volume_max) if info.volume_max is not None else None,
        "volume_step": str(info.volume_step) if info.volume_step is not None else None,
        "tradeable": info.tradeable,
    }


async def get_account_information(
    db: AsyncSession, tenant_id: str, trading_account_id: str, requesting_account_id: str | None = None
) -> dict[str, Any]:
    account = await _resolve_trading_account(db, tenant_id, trading_account_id, requesting_account_id)
    client = await get_metaapi_client(db)
    info = await client.get_account_information(account.metaapi_account_id)
    return {
        "source": info.source,
        "account_id": trading_account_id,
        "balance": str(info.balance),
        "equity": str(info.equity),
        "margin": str(info.margin),
        "free_margin": str(info.free_margin),
        "margin_level": str(info.margin_level) if info.margin_level is not None else None,
        "currency": info.currency,
        "leverage": info.leverage,
        "trade_allowed": info.trade_allowed,
    }


async def get_positions(
    db: AsyncSession, tenant_id: str, trading_account_id: str, requesting_account_id: str | None = None
) -> dict[str, Any]:
    account = await _resolve_trading_account(db, tenant_id, trading_account_id, requesting_account_id)
    client = await get_metaapi_client(db)
    positions = await client.get_positions(account.metaapi_account_id)
    return {
        "source": "metaapi",
        "account_id": trading_account_id,
        "positions": [_position_to_dict(p) for p in positions],
        "count": len(positions),
    }


async def get_orders(
    db: AsyncSession, tenant_id: str, trading_account_id: str, requesting_account_id: str | None = None
) -> dict[str, Any]:
    account = await _resolve_trading_account(db, tenant_id, trading_account_id, requesting_account_id)
    client = await get_metaapi_client(db)
    orders = await client.get_orders(account.metaapi_account_id)
    return {
        "source": "metaapi",
        "account_id": trading_account_id,
        "orders": [_order_to_dict(o) for o in orders],
        "count": len(orders),
    }


async def get_trade_history(
    db: AsyncSession,
    tenant_id: str,
    trading_account_id: str,
    start: datetime,
    end: datetime,
    requesting_account_id: str | None = None,
) -> dict[str, Any]:
    account = await _resolve_trading_account(db, tenant_id, trading_account_id, requesting_account_id)
    client = await get_metaapi_client(db)
    orders = await client.get_history_orders(account.metaapi_account_id, start, end)
    deals = await client.get_history_deals(account.metaapi_account_id, start, end)
    return {
        "source": "metaapi",
        "account_id": trading_account_id,
        "orders": [_order_to_dict(o) for o in orders],
        "deals": deals,
    }


async def calculate_margin(
    db: AsyncSession,
    tenant_id: str,
    trading_account_id: str,
    symbol: str,
    side: str,
    volume: str,
    order_type: str = "market",
    requesting_account_id: str | None = None,
) -> dict[str, Any]:
    account = await _resolve_trading_account(db, tenant_id, trading_account_id, requesting_account_id)
    client = await get_metaapi_client(db)
    estimate = await client.calculate_margin(
        account.metaapi_account_id, symbol, OrderSide(side), Decimal(str(volume)), OrderType(order_type)
    )
    return {
        "source": estimate.source,
        "account_id": trading_account_id,
        "symbol": estimate.symbol,
        "volume": str(estimate.volume),
        "margin": str(estimate.margin),
    }


def _position_to_dict(p: Any) -> dict[str, Any]:
    return {
        "position_id": p.position_id,
        "symbol": p.symbol,
        "side": p.side.value,
        "volume": str(p.volume),
        "open_price": str(p.open_price),
        "current_price": str(p.current_price) if p.current_price is not None else None,
        "stop_loss": str(p.stop_loss) if p.stop_loss is not None else None,
        "take_profit": str(p.take_profit) if p.take_profit is not None else None,
        "profit": str(p.profit) if p.profit is not None else None,
        "opened_at": p.opened_at.isoformat() if p.opened_at else None,
    }


def _order_to_dict(o: Any) -> dict[str, Any]:
    return {
        "order_id": o.order_id,
        "symbol": o.symbol,
        "side": o.side.value,
        "order_type": o.order_type.value,
        "volume": str(o.volume),
        "open_price": str(o.open_price) if o.open_price is not None else None,
        "stop_loss": str(o.stop_loss) if o.stop_loss is not None else None,
        "take_profit": str(o.take_profit) if o.take_profit is not None else None,
        "state": o.state,
        "created_at": o.created_at.isoformat() if o.created_at else None,
    }
