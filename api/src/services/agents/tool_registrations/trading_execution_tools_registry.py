"""
Trading Execution Tools Registry

Registers the propose_* tools — the only trading tools that can lead to a
real-money action, and even they only ever create a pending, human-approved
proposal (see internal_tools/trading_execution_tools.py). All tagged
tool_category="action" so the platform's HITL approval gate treats them
consistently with every other action-category tool.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

_TRADING_ACCOUNT_ID_PARAM = {
    "type": "string",
    "description": "The user's TradingAccount id, from internal_trading_list_accounts.",
}
_SIDE_PARAM = {"type": "string", "enum": ["buy", "sell"]}


def register_trading_execution_tools(registry):
    """Register the propose_* execution tools with the ADK tool registry."""
    from src.services.agents.internal_tools.trading_execution_tools import (
        internal_trading_get_pending_proposals,
        internal_trading_propose_cancel_order,
        internal_trading_propose_close_position,
        internal_trading_propose_limit_order,
        internal_trading_propose_market_order,
        internal_trading_propose_modify_position,
        internal_trading_propose_stop_order,
    )

    async def market_wrapper(config: dict[str, Any] | None = None, **kwargs):
        runtime_context = config.get("_runtime_context") if config else None
        return await internal_trading_propose_market_order(
            trading_account_id=kwargs.get("trading_account_id"),
            symbol=kwargs.get("symbol"),
            side=kwargs.get("side"),
            stop_loss=kwargs.get("stop_loss"),
            take_profit=kwargs.get("take_profit"),
            volume=kwargs.get("volume"),
            runtime_context=runtime_context,
        )

    async def limit_wrapper(config: dict[str, Any] | None = None, **kwargs):
        runtime_context = config.get("_runtime_context") if config else None
        return await internal_trading_propose_limit_order(
            trading_account_id=kwargs.get("trading_account_id"),
            symbol=kwargs.get("symbol"),
            side=kwargs.get("side"),
            limit_price=kwargs.get("limit_price"),
            stop_loss=kwargs.get("stop_loss"),
            take_profit=kwargs.get("take_profit"),
            volume=kwargs.get("volume"),
            runtime_context=runtime_context,
        )

    async def stop_wrapper(config: dict[str, Any] | None = None, **kwargs):
        runtime_context = config.get("_runtime_context") if config else None
        return await internal_trading_propose_stop_order(
            trading_account_id=kwargs.get("trading_account_id"),
            symbol=kwargs.get("symbol"),
            side=kwargs.get("side"),
            stop_price=kwargs.get("stop_price"),
            stop_loss=kwargs.get("stop_loss"),
            take_profit=kwargs.get("take_profit"),
            volume=kwargs.get("volume"),
            runtime_context=runtime_context,
        )

    async def close_wrapper(config: dict[str, Any] | None = None, **kwargs):
        runtime_context = config.get("_runtime_context") if config else None
        return await internal_trading_propose_close_position(
            trading_account_id=kwargs.get("trading_account_id"),
            position_id=kwargs.get("position_id"),
            volume=kwargs.get("volume"),
            symbol=kwargs.get("symbol"),
            runtime_context=runtime_context,
        )

    async def modify_wrapper(config: dict[str, Any] | None = None, **kwargs):
        runtime_context = config.get("_runtime_context") if config else None
        return await internal_trading_propose_modify_position(
            trading_account_id=kwargs.get("trading_account_id"),
            position_id=kwargs.get("position_id"),
            symbol=kwargs.get("symbol"),
            stop_loss=kwargs.get("stop_loss"),
            take_profit=kwargs.get("take_profit"),
            runtime_context=runtime_context,
        )

    async def cancel_wrapper(config: dict[str, Any] | None = None, **kwargs):
        runtime_context = config.get("_runtime_context") if config else None
        return await internal_trading_propose_cancel_order(
            trading_account_id=kwargs.get("trading_account_id"),
            order_id=kwargs.get("order_id"),
            symbol=kwargs.get("symbol"),
            runtime_context=runtime_context,
        )

    async def pending_wrapper(config: dict[str, Any] | None = None, **kwargs):
        runtime_context = config.get("_runtime_context") if config else None
        return await internal_trading_get_pending_proposals(
            trading_account_id=kwargs.get("trading_account_id"), runtime_context=runtime_context
        )

    registry.register_tool(
        name="internal_trading_propose_market_order",
        description=(
            "Propose a market order (immediate buy/sell) on the user's own linked MetaApi account. "
            "This does NOT execute anything — it creates a pending proposal that a human with approval "
            "authority must explicitly approve before any real order is placed. A stop-loss is always required."
        ),
        parameters={
            "type": "object",
            "properties": {
                "trading_account_id": _TRADING_ACCOUNT_ID_PARAM,
                "symbol": {"type": "string", "description": "Symbol in MetaApi's own naming convention."},
                "side": _SIDE_PARAM,
                "stop_loss": {"type": "string", "description": "Required stop-loss price."},
                "take_profit": {"type": "string", "description": "Optional take-profit price."},
                "volume": {
                    "type": "string",
                    "description": (
                        "Optional explicit volume/lot size. If omitted, volume is computed server-side from the "
                        "account's configured risk-per-trade setting — never left to the model to guess."
                    ),
                },
            },
            "required": ["trading_account_id", "symbol", "side", "stop_loss"],
        },
        function=market_wrapper,
        tool_category="action",
    )

    registry.register_tool(
        name="internal_trading_propose_limit_order",
        description=(
            "Propose a limit order (buy/sell only at a specified price or better) on the user's own linked "
            "MetaApi account. Creates a pending proposal — never executes directly. Stop-loss required."
        ),
        parameters={
            "type": "object",
            "properties": {
                "trading_account_id": _TRADING_ACCOUNT_ID_PARAM,
                "symbol": {"type": "string"},
                "side": _SIDE_PARAM,
                "limit_price": {"type": "string", "description": "The limit price."},
                "stop_loss": {"type": "string", "description": "Required stop-loss price."},
                "take_profit": {"type": "string", "description": "Optional take-profit price."},
                "volume": {"type": "string", "description": "Optional explicit volume (else risk-derived)."},
            },
            "required": ["trading_account_id", "symbol", "side", "limit_price", "stop_loss"],
        },
        function=limit_wrapper,
        tool_category="action",
    )

    registry.register_tool(
        name="internal_trading_propose_stop_order",
        description=(
            "Propose a stop order (buy/sell triggered once price reaches stop_price) on the user's own linked "
            "MetaApi account. Creates a pending proposal — never executes directly. Stop-loss required."
        ),
        parameters={
            "type": "object",
            "properties": {
                "trading_account_id": _TRADING_ACCOUNT_ID_PARAM,
                "symbol": {"type": "string"},
                "side": _SIDE_PARAM,
                "stop_price": {"type": "string", "description": "The trigger price."},
                "stop_loss": {"type": "string", "description": "Required stop-loss price."},
                "take_profit": {"type": "string", "description": "Optional take-profit price."},
                "volume": {"type": "string", "description": "Optional explicit volume (else risk-derived)."},
            },
            "required": ["trading_account_id", "symbol", "side", "stop_price", "stop_loss"],
        },
        function=stop_wrapper,
        tool_category="action",
    )

    registry.register_tool(
        name="internal_trading_propose_close_position",
        description="Propose closing an existing open position (fully or partially). Creates a pending proposal — never executes directly.",
        parameters={
            "type": "object",
            "properties": {
                "trading_account_id": _TRADING_ACCOUNT_ID_PARAM,
                "position_id": {
                    "type": "string",
                    "description": "The MetaApi position id to close (from internal_trading_get_positions).",
                },
                "symbol": {"type": "string", "description": "The position's symbol."},
                "volume": {
                    "type": "string",
                    "description": "Volume to close (equal to the position's full volume for a full close).",
                },
            },
            "required": ["trading_account_id", "position_id", "symbol", "volume"],
        },
        function=close_wrapper,
        tool_category="action",
    )

    registry.register_tool(
        name="internal_trading_propose_modify_position",
        description="Propose changing the stop-loss/take-profit on an existing open position. Creates a pending proposal — never executes directly.",
        parameters={
            "type": "object",
            "properties": {
                "trading_account_id": _TRADING_ACCOUNT_ID_PARAM,
                "position_id": {"type": "string", "description": "The MetaApi position id to modify."},
                "symbol": {"type": "string"},
                "stop_loss": {"type": "string", "description": "New stop-loss price (optional if take_profit given)."},
                "take_profit": {
                    "type": "string",
                    "description": "New take-profit price (optional if stop_loss given).",
                },
            },
            "required": ["trading_account_id", "position_id", "symbol"],
        },
        function=modify_wrapper,
        tool_category="action",
    )

    registry.register_tool(
        name="internal_trading_propose_cancel_order",
        description="Propose cancelling an existing pending order. Creates a pending proposal — never executes directly.",
        parameters={
            "type": "object",
            "properties": {
                "trading_account_id": _TRADING_ACCOUNT_ID_PARAM,
                "order_id": {"type": "string", "description": "The MetaApi order id to cancel."},
                "symbol": {"type": "string"},
            },
            "required": ["trading_account_id", "order_id", "symbol"],
        },
        function=cancel_wrapper,
        tool_category="action",
    )

    registry.register_tool(
        name="internal_trading_get_pending_proposals",
        description="List the user's own pending trade proposals that are awaiting human approval.",
        parameters={
            "type": "object",
            "properties": {"trading_account_id": _TRADING_ACCOUNT_ID_PARAM},
            "required": [],
        },
        function=pending_wrapper,
    )

    logger.info("Registered 7 trading execution/proposal tools")
