"""
Trading Tools Registry (read-only)

Registers read-only market-data and account-inspection tools with the ADK
tool registry. Execution tools (proposals) are registered separately
(trading_execution_tools_registry.py, Phase 5) and are the only trading
tools tagged tool_category="action" — everything here is safe to call
without any human approval gate.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

_SOURCE_PARAM = {
    "type": "string",
    "enum": ["oanda", "metaapi"],
    "description": (
        "Data source. 'oanda' = platform-level market data, no account needed, always available. "
        "'metaapi' = the user's own linked broker account (requires trading_account_id) — use this only "
        "when the user is asking about their real account, not for general analysis."
    ),
}
_TRADING_ACCOUNT_ID_PARAM = {
    "type": "string",
    "description": (
        "The user's TradingAccount id (from internal_trading_list_accounts). Required when source='metaapi'; "
        "not used when source='oanda'."
    ),
}


def register_trading_tools(registry):
    """Register all read-only trading tools with the ADK tool registry."""
    from src.services.agents.internal_tools.trading_tools import (
        internal_trading_calculate_margin,
        internal_trading_get_account_information,
        internal_trading_get_candles,
        internal_trading_get_instruments,
        internal_trading_get_orders,
        internal_trading_get_positions,
        internal_trading_get_quote,
        internal_trading_get_symbol_information,
        internal_trading_get_trade_history,
        internal_trading_list_accounts,
    )

    async def list_accounts_wrapper(config: dict[str, Any] | None = None, **_kwargs):
        runtime_context = config.get("_runtime_context") if config else None
        return await internal_trading_list_accounts(runtime_context=runtime_context)

    async def get_quote_wrapper(config: dict[str, Any] | None = None, **kwargs):
        runtime_context = config.get("_runtime_context") if config else None
        return await internal_trading_get_quote(
            source=kwargs.get("source"),
            symbol=kwargs.get("symbol"),
            trading_account_id=kwargs.get("trading_account_id"),
            runtime_context=runtime_context,
        )

    async def get_candles_wrapper(config: dict[str, Any] | None = None, **kwargs):
        runtime_context = config.get("_runtime_context") if config else None
        return await internal_trading_get_candles(
            source=kwargs.get("source"),
            symbol=kwargs.get("symbol"),
            timeframe=kwargs.get("timeframe"),
            start=kwargs.get("start"),
            end=kwargs.get("end"),
            limit=kwargs.get("limit"),
            trading_account_id=kwargs.get("trading_account_id"),
            runtime_context=runtime_context,
        )

    async def get_instruments_wrapper(config: dict[str, Any] | None = None, **kwargs):
        runtime_context = config.get("_runtime_context") if config else None
        return await internal_trading_get_instruments(
            source=kwargs.get("source"),
            trading_account_id=kwargs.get("trading_account_id"),
            runtime_context=runtime_context,
        )

    async def get_symbol_information_wrapper(config: dict[str, Any] | None = None, **kwargs):
        runtime_context = config.get("_runtime_context") if config else None
        return await internal_trading_get_symbol_information(
            source=kwargs.get("source"),
            symbol=kwargs.get("symbol"),
            trading_account_id=kwargs.get("trading_account_id"),
            runtime_context=runtime_context,
        )

    async def get_account_information_wrapper(config: dict[str, Any] | None = None, **kwargs):
        runtime_context = config.get("_runtime_context") if config else None
        return await internal_trading_get_account_information(
            trading_account_id=kwargs.get("trading_account_id"), runtime_context=runtime_context
        )

    async def get_positions_wrapper(config: dict[str, Any] | None = None, **kwargs):
        runtime_context = config.get("_runtime_context") if config else None
        return await internal_trading_get_positions(
            trading_account_id=kwargs.get("trading_account_id"), runtime_context=runtime_context
        )

    async def get_orders_wrapper(config: dict[str, Any] | None = None, **kwargs):
        runtime_context = config.get("_runtime_context") if config else None
        return await internal_trading_get_orders(
            trading_account_id=kwargs.get("trading_account_id"), runtime_context=runtime_context
        )

    async def get_trade_history_wrapper(config: dict[str, Any] | None = None, **kwargs):
        runtime_context = config.get("_runtime_context") if config else None
        return await internal_trading_get_trade_history(
            trading_account_id=kwargs.get("trading_account_id"),
            start=kwargs.get("start"),
            end=kwargs.get("end"),
            runtime_context=runtime_context,
        )

    async def calculate_margin_wrapper(config: dict[str, Any] | None = None, **kwargs):
        runtime_context = config.get("_runtime_context") if config else None
        return await internal_trading_calculate_margin(
            trading_account_id=kwargs.get("trading_account_id"),
            symbol=kwargs.get("symbol"),
            side=kwargs.get("side"),
            volume=kwargs.get("volume"),
            order_type=kwargs.get("order_type", "market"),
            runtime_context=runtime_context,
        )

    registry.register_tool(
        name="internal_trading_list_accounts",
        description="List the current user's own linked MetaApi trading accounts (id, label, connection status). Call this before any tool that needs a trading_account_id, unless the user already gave you one.",
        parameters={"type": "object", "properties": {}, "required": []},
        function=list_accounts_wrapper,
    )

    registry.register_tool(
        name="internal_trading_get_quote",
        description="Get a live bid/ask/spread quote for a symbol. Use source='oanda' for general market analysis and recommendations (no account needed). Only use source='metaapi' when the user is specifically asking about the price on their own broker account.",
        parameters={
            "type": "object",
            "properties": {
                "source": _SOURCE_PARAM,
                "symbol": {
                    "type": "string",
                    "description": "Symbol in the source's own naming convention (e.g. 'EUR_USD' for oanda, 'EURUSD' for metaapi).",
                },
                "trading_account_id": _TRADING_ACCOUNT_ID_PARAM,
            },
            "required": ["source", "symbol"],
        },
        function=get_quote_wrapper,
    )

    registry.register_tool(
        name="internal_trading_get_candles",
        description="Get historical OHLC candles for a symbol/timeframe. Use source='oanda' for analysis and recommendations by default.",
        parameters={
            "type": "object",
            "properties": {
                "source": _SOURCE_PARAM,
                "symbol": {"type": "string", "description": "Symbol in the source's own naming convention."},
                "timeframe": {
                    "type": "string",
                    "enum": ["M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"],
                    "description": "Candle timeframe.",
                },
                "start": {"type": "string", "description": "ISO-8601 start time (optional)."},
                "end": {"type": "string", "description": "ISO-8601 end time (optional)."},
                "limit": {"type": "integer", "description": "Max number of candles to return (optional)."},
                "trading_account_id": _TRADING_ACCOUNT_ID_PARAM,
            },
            "required": ["source", "symbol", "timeframe"],
        },
        function=get_candles_wrapper,
    )

    registry.register_tool(
        name="internal_trading_get_instruments",
        description="List tradeable symbols for a data source.",
        parameters={
            "type": "object",
            "properties": {"source": _SOURCE_PARAM, "trading_account_id": _TRADING_ACCOUNT_ID_PARAM},
            "required": ["source"],
        },
        function=get_instruments_wrapper,
    )

    registry.register_tool(
        name="internal_trading_get_symbol_information",
        description="Get a symbol's tradable specification: minimum/maximum/step volume and whether it's currently tradeable.",
        parameters={
            "type": "object",
            "properties": {
                "source": _SOURCE_PARAM,
                "symbol": {"type": "string", "description": "Symbol in the source's own naming convention."},
                "trading_account_id": _TRADING_ACCOUNT_ID_PARAM,
            },
            "required": ["source", "symbol"],
        },
        function=get_symbol_information_wrapper,
    )

    registry.register_tool(
        name="internal_trading_get_account_information",
        description="Get balance, equity, margin, and free margin for the user's own linked MetaApi account. Requires a trading_account_id from internal_trading_list_accounts.",
        parameters={
            "type": "object",
            "properties": {"trading_account_id": _TRADING_ACCOUNT_ID_PARAM},
            "required": ["trading_account_id"],
        },
        function=get_account_information_wrapper,
    )

    registry.register_tool(
        name="internal_trading_get_positions",
        description="List open positions on the user's own linked MetaApi account.",
        parameters={
            "type": "object",
            "properties": {"trading_account_id": _TRADING_ACCOUNT_ID_PARAM},
            "required": ["trading_account_id"],
        },
        function=get_positions_wrapper,
    )

    registry.register_tool(
        name="internal_trading_get_orders",
        description="List pending (unfilled) orders on the user's own linked MetaApi account.",
        parameters={
            "type": "object",
            "properties": {"trading_account_id": _TRADING_ACCOUNT_ID_PARAM},
            "required": ["trading_account_id"],
        },
        function=get_orders_wrapper,
    )

    registry.register_tool(
        name="internal_trading_get_trade_history",
        description="Get executed order/deal history for the user's own linked MetaApi account within a time range.",
        parameters={
            "type": "object",
            "properties": {
                "trading_account_id": _TRADING_ACCOUNT_ID_PARAM,
                "start": {"type": "string", "description": "ISO-8601 start time."},
                "end": {"type": "string", "description": "ISO-8601 end time."},
            },
            "required": ["trading_account_id", "start", "end"],
        },
        function=get_trade_history_wrapper,
    )

    registry.register_tool(
        name="internal_trading_calculate_margin",
        description="Estimate the margin required for a hypothetical order on the user's own linked MetaApi account. Informational only — does not place anything.",
        parameters={
            "type": "object",
            "properties": {
                "trading_account_id": _TRADING_ACCOUNT_ID_PARAM,
                "symbol": {"type": "string", "description": "Symbol in MetaApi's own naming convention."},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "volume": {"type": "string", "description": "Order volume/lot size, e.g. '0.1'."},
                "order_type": {"type": "string", "enum": ["market", "limit", "stop"], "default": "market"},
            },
            "required": ["trading_account_id", "symbol", "side", "volume"],
        },
        function=calculate_margin_wrapper,
    )

    logger.info("Registered 10 read-only trading tools")
