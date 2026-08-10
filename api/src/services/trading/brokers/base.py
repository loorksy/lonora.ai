"""
Broker adapter interfaces for the trading domain.

Two independent adapter shapes, matching the platform's architecture split
between market data (OANDA, platform-level, no user account required) and
execution (MetaApi, strictly per-user linked account):

    Agent -> Trading tools -> Trading domain services -> Broker adapter -> MetaApi/OANDA

Broker-specific request/response shapes and symbol conventions never leak
past this layer — callers always get back the plain dataclasses defined
here, tagged with an explicit `source` field ("oanda" | "metaapi") so a
caller can never accidentally mix or silently conflate prices/data from the
two venues (see symbol_mapping.py for the explicit OANDA<->MetaApi symbol
normalization this implies).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class BrokerError(Exception):
    """Base class for all broker adapter errors. Never fabricate data on error — raise instead."""


class BrokerAuthError(BrokerError):
    """Credential missing, invalid, or rejected by the broker."""


class BrokerTimeoutError(BrokerError):
    """The broker did not respond in time. Never silently retried into fabricated data."""


class BrokerRequestError(BrokerError):
    """The broker rejected the request (bad symbol, invalid params, etc.)."""


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class Quote:
    source: str  # "oanda" | "metaapi"
    symbol: str  # broker-native symbol, as returned by that broker
    bid: Decimal
    ask: Decimal
    spread: Decimal
    timestamp: datetime
    tradeable: bool = True


@dataclass
class Candle:
    time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None


@dataclass
class CandleSeries:
    source: str
    symbol: str
    timeframe: str
    candles: list[Candle] = field(default_factory=list)


@dataclass
class SymbolInfo:
    source: str
    symbol: str
    description: str | None = None
    pip_size: Decimal | None = None
    volume_min: Decimal | None = None
    volume_max: Decimal | None = None
    volume_step: Decimal | None = None
    # Units per 1.0 volume — required to convert price deltas to account-currency risk.
    contract_size: Decimal | None = None
    tradeable: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class AccountInformation:
    source: str  # always "metaapi" — account state only ever comes from the user's own broker
    account_id: str
    balance: Decimal
    equity: Decimal
    margin: Decimal
    free_margin: Decimal
    margin_level: Decimal | None
    currency: str
    leverage: int | None = None
    trade_allowed: bool = True


@dataclass
class Position:
    source: str
    position_id: str
    symbol: str
    side: OrderSide
    volume: Decimal
    open_price: Decimal
    current_price: Decimal | None
    stop_loss: Decimal | None
    take_profit: Decimal | None
    profit: Decimal | None
    opened_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Order:
    source: str
    order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    volume: Decimal
    open_price: Decimal | None
    stop_loss: Decimal | None
    take_profit: Decimal | None
    state: str | None = None
    created_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class MarginEstimate:
    source: str
    symbol: str
    volume: Decimal
    margin: Decimal
    currency: str


@dataclass
class BrokerOrderResult:
    source: str
    broker_order_id: str | None
    position_id: str | None
    status: str  # broker-reported status string
    price: Decimal | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class MarketDataAdapter(ABC):
    """Read-only market data — no user account required (OANDA today)."""

    source: str

    @abstractmethod
    async def get_instruments(self) -> list[SymbolInfo]: ...

    @abstractmethod
    async def get_quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    async def get_candles(
        self,
        symbol: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> CandleSeries: ...

    @abstractmethod
    async def get_account_summary(self) -> dict[str, Any]:
        """Summary of the platform's own OANDA account — not any end user's account."""
        ...


class ExecutionBrokerAdapter(ABC):
    """Per-user linked execution account (MetaApi today). Never called without an explicit account reference."""

    source: str

    @abstractmethod
    async def get_account_information(self, account_ref: str) -> AccountInformation: ...

    @abstractmethod
    async def get_symbols(self, account_ref: str) -> list[str]: ...

    @abstractmethod
    async def get_symbol_information(self, account_ref: str, symbol: str) -> SymbolInfo: ...

    @abstractmethod
    async def get_quote(self, account_ref: str, symbol: str) -> Quote: ...

    @abstractmethod
    async def get_candles(
        self,
        account_ref: str,
        symbol: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> CandleSeries: ...

    @abstractmethod
    async def get_positions(self, account_ref: str) -> list[Position]: ...

    @abstractmethod
    async def get_orders(self, account_ref: str) -> list[Order]: ...

    @abstractmethod
    async def get_history_orders(self, account_ref: str, start: datetime, end: datetime) -> list[Order]: ...

    @abstractmethod
    async def get_history_deals(self, account_ref: str, start: datetime, end: datetime) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def calculate_margin(
        self, account_ref: str, symbol: str, side: OrderSide, volume: Decimal, order_type: OrderType = OrderType.MARKET
    ) -> MarginEstimate: ...

    # -- Execution (only ever called after human approval — see execution_service.py) --

    @abstractmethod
    async def create_order(
        self,
        account_ref: str,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        volume: Decimal,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
        limit_price: Decimal | None = None,
    ) -> BrokerOrderResult: ...

    @abstractmethod
    async def modify_order(
        self,
        account_ref: str,
        order_id: str,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
        limit_price: Decimal | None = None,
    ) -> BrokerOrderResult: ...

    @abstractmethod
    async def cancel_order(self, account_ref: str, order_id: str) -> BrokerOrderResult: ...

    @abstractmethod
    async def modify_position(
        self, account_ref: str, position_id: str, stop_loss: Decimal | None = None, take_profit: Decimal | None = None
    ) -> BrokerOrderResult: ...

    @abstractmethod
    async def close_position(self, account_ref: str, position_id: str) -> BrokerOrderResult: ...
