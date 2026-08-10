"""
Trading Tools (read-only) — market data and account/position/order inspection.

Every tool here is read-only: no tool in this file can place, modify,
cancel, or close anything. Execution tools live in a separate module
(trading_execution_tools.py, added in Phase 5) and are gated by the
platform's HITL approval system — never called directly from here.

Tools clearly tag every response with its data source ("oanda" for
platform-level market data, "metaapi" for the caller's own linked account)
and never fabricate values: broker/lookup failures are returned as
{"success": False, "error": "..."} rather than invented numbers, per §7/§13
of the trading domain spec.
"""

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select

from src.core.database import get_async_db
from src.models.trading_account import TradingAccount
from src.services.trading import market_data_service
from src.services.trading.brokers.base import BrokerError

logger = logging.getLogger(__name__)


def _context_ids(runtime_context: Any) -> tuple[str | None, str | None]:
    """Extract (tenant_id, user_id) from either a RuntimeContext object or a plain dict."""
    if runtime_context is None:
        return None, None
    if isinstance(runtime_context, dict):
        tenant_id = runtime_context.get("tenant_id")
        user_id = runtime_context.get("user_id")
    else:
        tenant_id = getattr(runtime_context, "tenant_id", None)
        user_id = getattr(runtime_context, "user_id", None)
    return (str(tenant_id) if tenant_id else None), (str(user_id) if user_id else None)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def internal_trading_list_accounts(runtime_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """List the calling user's own linked MetaApi trading accounts (id, label, status)."""
    tenant_id, user_id = _context_ids(runtime_context)
    if not tenant_id:
        return {"success": False, "error": "Missing tenant_id in runtime context"}

    try:
        async for db in get_async_db():
            stmt = select(TradingAccount).where(TradingAccount.tenant_id == uuid.UUID(tenant_id))
            if user_id:
                stmt = stmt.where(TradingAccount.account_id == uuid.UUID(user_id))
            result = await db.execute(stmt)
            accounts = result.scalars().all()
            return {
                "success": True,
                "accounts": [
                    {
                        "trading_account_id": str(a.id),
                        "label": a.label,
                        "status": a.status,
                        "mt_login": a.mt_login,
                        "mt_server": a.mt_server,
                    }
                    for a in accounts
                ],
            }
    except Exception as e:
        logger.warning(f"Error listing trading accounts: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def internal_trading_get_quote(
    source: str, symbol: str, trading_account_id: str | None = None, runtime_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Get a live quote (bid/ask/spread) for a symbol. source='oanda' needs no account; 'metaapi' needs trading_account_id."""
    tenant_id, user_id = _context_ids(runtime_context)
    if not tenant_id:
        return {"success": False, "error": "Missing tenant_id in runtime context"}
    try:
        async for db in get_async_db():
            quote = await market_data_service.get_quote(
                db, tenant_id, source, symbol, trading_account_id=trading_account_id, requesting_account_id=user_id
            )
            return {"success": True, **quote}
    except (BrokerError, ValueError) as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.warning(f"Error getting quote: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def internal_trading_get_candles(
    source: str,
    symbol: str,
    timeframe: str,
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
    trading_account_id: str | None = None,
    runtime_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Get historical OHLC candles. Timeframe: M1|M5|M15|M30|H1|H4|D1|W1. start/end are ISO-8601 timestamps."""
    tenant_id, user_id = _context_ids(runtime_context)
    if not tenant_id:
        return {"success": False, "error": "Missing tenant_id in runtime context"}
    try:
        async for db in get_async_db():
            series = await market_data_service.get_candles(
                db,
                tenant_id,
                source,
                symbol,
                timeframe,
                start=_parse_time(start),
                end=_parse_time(end),
                limit=limit,
                trading_account_id=trading_account_id,
                requesting_account_id=user_id,
            )
            return {"success": True, **series}
    except (BrokerError, ValueError) as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.warning(f"Error getting candles: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def internal_trading_get_instruments(
    source: str, trading_account_id: str | None = None, runtime_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """List tradeable symbols for a source."""
    tenant_id, user_id = _context_ids(runtime_context)
    if not tenant_id:
        return {"success": False, "error": "Missing tenant_id in runtime context"}
    try:
        async for db in get_async_db():
            result = await market_data_service.get_instruments(
                db, tenant_id, source, trading_account_id=trading_account_id, requesting_account_id=user_id
            )
            return {"success": True, **result}
    except (BrokerError, ValueError) as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.warning(f"Error listing instruments: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def internal_trading_get_symbol_information(
    source: str, symbol: str, trading_account_id: str | None = None, runtime_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Get symbol specification (min/max/step volume, tradeable state)."""
    tenant_id, user_id = _context_ids(runtime_context)
    if not tenant_id:
        return {"success": False, "error": "Missing tenant_id in runtime context"}
    try:
        async for db in get_async_db():
            info = await market_data_service.get_symbol_information(
                db, tenant_id, source, symbol, trading_account_id=trading_account_id, requesting_account_id=user_id
            )
            return {"success": True, **info}
    except (BrokerError, ValueError) as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.warning(f"Error getting symbol information: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def internal_trading_get_account_information(
    trading_account_id: str, runtime_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Get balance/equity/margin for the caller's own linked MetaApi account."""
    tenant_id, user_id = _context_ids(runtime_context)
    if not tenant_id:
        return {"success": False, "error": "Missing tenant_id in runtime context"}
    try:
        async for db in get_async_db():
            info = await market_data_service.get_account_information(
                db, tenant_id, trading_account_id, requesting_account_id=user_id
            )
            return {"success": True, **info}
    except (BrokerError, ValueError) as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.warning(f"Error getting account information: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def internal_trading_get_positions(
    trading_account_id: str, runtime_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """List open positions on the caller's own linked MetaApi account."""
    tenant_id, user_id = _context_ids(runtime_context)
    if not tenant_id:
        return {"success": False, "error": "Missing tenant_id in runtime context"}
    try:
        async for db in get_async_db():
            result = await market_data_service.get_positions(
                db, tenant_id, trading_account_id, requesting_account_id=user_id
            )
            return {"success": True, **result}
    except (BrokerError, ValueError) as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.warning(f"Error getting positions: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def internal_trading_get_orders(
    trading_account_id: str, runtime_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """List pending orders on the caller's own linked MetaApi account."""
    tenant_id, user_id = _context_ids(runtime_context)
    if not tenant_id:
        return {"success": False, "error": "Missing tenant_id in runtime context"}
    try:
        async for db in get_async_db():
            result = await market_data_service.get_orders(
                db, tenant_id, trading_account_id, requesting_account_id=user_id
            )
            return {"success": True, **result}
    except (BrokerError, ValueError) as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.warning(f"Error getting orders: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def internal_trading_get_trade_history(
    trading_account_id: str, start: str, end: str, runtime_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Get executed order/deal history for the caller's own linked MetaApi account within a time range."""
    tenant_id, user_id = _context_ids(runtime_context)
    if not tenant_id:
        return {"success": False, "error": "Missing tenant_id in runtime context"}
    parsed_start, parsed_end = _parse_time(start), _parse_time(end)
    if parsed_start is None or parsed_end is None:
        return {"success": False, "error": "Both start and end are required (ISO-8601 timestamps)"}
    try:
        async for db in get_async_db():
            result = await market_data_service.get_trade_history(
                db, tenant_id, trading_account_id, parsed_start, parsed_end, requesting_account_id=user_id
            )
            return {"success": True, **result}
    except (BrokerError, ValueError) as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.warning(f"Error getting trade history: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def internal_trading_calculate_margin(
    trading_account_id: str,
    symbol: str,
    side: str,
    volume: str,
    order_type: str = "market",
    runtime_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Estimate the margin required for a hypothetical order. Does not place anything."""
    tenant_id, user_id = _context_ids(runtime_context)
    if not tenant_id:
        return {"success": False, "error": "Missing tenant_id in runtime context"}
    try:
        async for db in get_async_db():
            result = await market_data_service.calculate_margin(
                db, tenant_id, trading_account_id, symbol, side, volume, order_type, requesting_account_id=user_id
            )
            return {"success": True, **result}
    except (BrokerError, ValueError) as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.warning(f"Error calculating margin: {e}", exc_info=True)
        return {"success": False, "error": str(e)}
