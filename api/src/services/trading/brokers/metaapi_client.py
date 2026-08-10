"""
MetaApi (MetaTrader 4/5 cloud API) adapter — the platform's execution broker.

Every call here targets one specific user's linked MT account
(TradingAccount.metaapi_account_id); there is no platform-wide MetaApi
"market data" use — that role belongs entirely to OandaClient (§4.2 of the
trading domain spec). This adapter is only ever invoked (a) for read-only
account/position/order inspection of a MetaApi-linked user, and (b) for
execution, and execution is only ever reached after human approval (see
src/services/trading/execution_service.py) — nothing in this file is called
directly by an agent tool.

Implemented against MetaApi's plain REST **Client API** (RPC-style — a
regular request/response per call, no websocket/streaming session), per the
platform spec's instruction not to use streaming connections without a
demonstrated need:

  Client API   (per-request trading-terminal state / trade execution):
      https://mt-client-api-v1.{region}.agiliumtrade.ai/users/current/accounts/{accountId}/...
  Provisioning API (account linking / region lookup):
      https://mt-provisioning-api-v1.agiliumtrade.ai/users/current/accounts...

  Auth: `auth-token: <token>` header (NOT `Authorization: Bearer`) on both
  hosts, using the single platform-owned MetaApi token
  (PlatformBrokerCredential, broker='metaapi') — end users never hold or
  provide their own MetaApi API token, only their MT login/password/server.

Endpoints below marked "confirmed" were verified against MetaApi's public
docs at implementation time (account-information, positions, orders,
current-price, current-candles/{timeframe}, calculate-margin, trade,
provisioning account create/read). The history-orders/history-deals
time-range paths follow the same documented naming convention as their
confirmed siblings but should be smoke-tested against a real MetaApi demo
account before relying on them in production, since this session had no
live network access to MetaApi's docs host to double-check byte-for-byte.
"""

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from src.services.oauth.http_client import RETRYABLE_STATUS_CODES, calculate_backoff_delay
from src.services.trading.brokers.base import (
    AccountInformation,
    BrokerAuthError,
    BrokerOrderResult,
    BrokerRequestError,
    BrokerTimeoutError,
    Candle,
    CandleSeries,
    ExecutionBrokerAdapter,
    MarginEstimate,
    Order,
    OrderSide,
    OrderType,
    Position,
    Quote,
    SymbolInfo,
)
from src.services.trading.timeframes import validate_timeframe

logger = logging.getLogger(__name__)

_PROVISIONING_BASE_URL = "https://mt-provisioning-api-v1.agiliumtrade.ai"
_CLIENT_API_HOST_TEMPLATE = "https://mt-client-api-v1.{region}.agiliumtrade.ai"

# Canonical timeframe -> MetaApi candle timeframe string.
_TIMEFRAME_TO_METAAPI = {
    "M1": "1m",
    "M5": "5m",
    "M15": "15m",
    "M30": "30m",
    "H1": "1h",
    "H4": "4h",
    "D1": "1d",
    "W1": "1w",
}

_ACTION_TYPE_MARKET = {OrderSide.BUY: "ORDER_TYPE_BUY", OrderSide.SELL: "ORDER_TYPE_SELL"}
_ACTION_TYPE_LIMIT = {OrderSide.BUY: "ORDER_TYPE_BUY_LIMIT", OrderSide.SELL: "ORDER_TYPE_SELL_LIMIT"}
_ACTION_TYPE_STOP = {OrderSide.BUY: "ORDER_TYPE_BUY_STOP", OrderSide.SELL: "ORDER_TYPE_SELL_STOP"}

_REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
_MAX_RETRIES = 3


class MetaApiClient(ExecutionBrokerAdapter):
    """MetaApi execution adapter. One instance per platform token; `account_ref` selects the MT account per call."""

    source = "metaapi"

    def __init__(self, token: str):
        if not token:
            raise BrokerAuthError("MetaApi token is required")
        self._token = token
        self._region_cache: dict[str, str] = {}

    def _headers(self) -> dict[str, str]:
        return {"auth-token": self._token, "Content-Type": "application/json", "Accept": "application/json"}

    async def _request(self, method: str, base_url: str, path: str, **kwargs: Any) -> Any:
        # Only GET is safe to blanket-retry: it's idempotent, so a retry after a timeout can never
        # cause a duplicate side effect. POST/PUT/DELETE here include order placement/modification/
        # cancellation (create_order etc.) — a timeout doesn't tell us whether MetaApi already acted
        # on the request, so retrying could place a duplicate order. Those keep the original
        # fail-fast (no retry) behavior; execution_service.py's proposal-level idempotency (CAS +
        # unique index + Redis token) guards against duplicate *approval*, not a retried broker call.
        url = f"{base_url}{path}"
        if method != "GET":
            try:
                async with httpx.AsyncClient(headers=self._headers(), timeout=_REQUEST_TIMEOUT) as client:
                    resp = await client.request(method, url, **kwargs)
            except httpx.TimeoutException as exc:
                raise BrokerTimeoutError(f"MetaApi request timed out: {method} {path}") from exc
            except httpx.HTTPError as exc:
                raise BrokerRequestError(f"MetaApi request failed: {exc}") from exc
        else:
            last_exc: Exception | None = None
            resp = None
            for attempt in range(_MAX_RETRIES + 1):
                try:
                    async with httpx.AsyncClient(headers=self._headers(), timeout=_REQUEST_TIMEOUT) as client:
                        resp = await client.request(method, url, **kwargs)
                except httpx.TimeoutException as exc:
                    last_exc = exc
                    if attempt >= _MAX_RETRIES:
                        raise BrokerTimeoutError(f"MetaApi request timed out: {method} {path}") from exc
                except httpx.HTTPError as exc:
                    raise BrokerRequestError(f"MetaApi request failed: {exc}") from exc
                else:
                    if resp.status_code not in RETRYABLE_STATUS_CODES or attempt >= _MAX_RETRIES:
                        break
                    last_exc = None

                delay = calculate_backoff_delay(attempt)
                logger.info(f"Retrying MetaApi GET {path} in {delay:.2f}s (attempt {attempt + 1}/{_MAX_RETRIES})")
                await asyncio.sleep(delay)

            if resp is None:
                raise BrokerTimeoutError(f"MetaApi request timed out: {method} {path}") from last_exc

        if resp.status_code == 401:
            raise BrokerAuthError("MetaApi rejected the platform credential (401)")
        if resp.status_code == 404:
            raise BrokerRequestError(f"MetaApi account/resource not found for {path}")
        if resp.status_code >= 400:
            raise BrokerRequestError(f"MetaApi returned {resp.status_code} for {path}: {resp.text[:300]}")
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    # ------------------------------------------------------------------
    # Region resolution + client-api routing
    # ------------------------------------------------------------------

    async def _get_region(self, account_ref: str) -> str:
        if account_ref in self._region_cache:
            return self._region_cache[account_ref]
        data = await self._request("GET", _PROVISIONING_BASE_URL, f"/users/current/accounts/{account_ref}")
        region = data.get("region")
        if not region:
            raise BrokerRequestError(f"MetaApi account {account_ref} has no region assigned yet (still deploying?)")
        self._region_cache[account_ref] = region
        return region

    async def _client_api_base(self, account_ref: str) -> str:
        region = await self._get_region(account_ref)
        return _CLIENT_API_HOST_TEMPLATE.format(region=region)

    async def _client_get(self, account_ref: str, path: str, params: dict[str, Any] | None = None) -> Any:
        base = await self._client_api_base(account_ref)
        return await self._request("GET", base, f"/users/current/accounts/{account_ref}{path}", params=params)

    async def _client_post(self, account_ref: str, path: str, json_body: dict[str, Any]) -> Any:
        base = await self._client_api_base(account_ref)
        return await self._request("POST", base, f"/users/current/accounts/{account_ref}{path}", json=json_body)

    # ------------------------------------------------------------------
    # Account linking / provisioning (§4.1 — not part of ExecutionBrokerAdapter,
    # since it manages the link itself rather than trading against one)
    # ------------------------------------------------------------------

    async def link_account(self, login: str, password: str, server: str, name: str) -> str:
        """Provision a new MetaApi account resource for a user's MT login. Returns the MetaApi account id."""
        body = {
            "login": login,
            "password": password,
            "server": server,
            "name": name,
            "type": "cloud",
            "platform": "mt5",
            "magic": 0,
        }
        data = await self._request("POST", _PROVISIONING_BASE_URL, "/users/current/accounts", json=body)
        account_id = data.get("id")
        if not account_id:
            raise BrokerRequestError(f"MetaApi did not return an account id for the new link: {data}")
        return account_id

    async def get_account_state(self, account_ref: str) -> dict[str, Any]:
        """Raw provisioning-side account state (deployment state, connection status, region)."""
        return await self._request("GET", _PROVISIONING_BASE_URL, f"/users/current/accounts/{account_ref}")

    async def deploy_account(self, account_ref: str) -> None:
        await self._request("POST", _PROVISIONING_BASE_URL, f"/users/current/accounts/{account_ref}/deploy", json={})

    async def remove_account(self, account_ref: str) -> None:
        await self._request("DELETE", _PROVISIONING_BASE_URL, f"/users/current/accounts/{account_ref}")
        self._region_cache.pop(account_ref, None)

    # ------------------------------------------------------------------
    # Read-only trading terminal state
    # ------------------------------------------------------------------

    async def get_account_information(self, account_ref: str) -> AccountInformation:
        data = await self._client_get(account_ref, "/account-information")
        return AccountInformation(
            source=self.source,
            account_id=account_ref,
            balance=Decimal(str(data["balance"])),
            equity=Decimal(str(data["equity"])),
            margin=Decimal(str(data.get("margin", 0))),
            free_margin=Decimal(str(data.get("freeMargin", 0))),
            margin_level=Decimal(str(data["marginLevel"])) if data.get("marginLevel") is not None else None,
            currency=data.get("currency", ""),
            leverage=data.get("leverage"),
            trade_allowed=data.get("tradeAllowed", True),
        )

    async def get_symbols(self, account_ref: str) -> list[str]:
        data = await self._client_get(account_ref, "/symbols")
        return list(data) if isinstance(data, list) else []

    async def get_symbol_information(self, account_ref: str, symbol: str) -> SymbolInfo:
        data = await self._client_get(account_ref, f"/symbols/{symbol}/specification")
        return SymbolInfo(
            source=self.source,
            symbol=symbol,
            description=data.get("description"),
            volume_min=Decimal(str(data["minVolume"])) if "minVolume" in data else None,
            volume_max=Decimal(str(data["maxVolume"])) if "maxVolume" in data else None,
            volume_step=Decimal(str(data["volumeStep"])) if "volumeStep" in data else None,
            contract_size=Decimal(str(data["contractSize"])) if "contractSize" in data else None,
            tradeable=data.get("tradeMode", 1) != 0,
            raw=data,
        )

    async def get_quote(self, account_ref: str, symbol: str) -> Quote:
        data = await self._client_get(account_ref, f"/symbols/{symbol}/current-price")
        bid = Decimal(str(data["bid"]))
        ask = Decimal(str(data["ask"]))
        return Quote(
            source=self.source,
            symbol=symbol,
            bid=bid,
            ask=ask,
            spread=ask - bid,
            timestamp=_parse_metaapi_time(data.get("time")),
        )

    async def get_candles(
        self,
        account_ref: str,
        symbol: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> CandleSeries:
        canonical_tf = validate_timeframe(timeframe)
        metaapi_tf = _TIMEFRAME_TO_METAAPI[canonical_tf]
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = min(limit, 1000)
        if start is not None:
            params["startTime"] = _to_metaapi_time(start)
        data = await self._client_get(account_ref, f"/symbols/{symbol}/current-candles/{metaapi_tf}", params=params)
        raw_candles = data if isinstance(data, list) else [data] if data else []
        candles = [
            Candle(
                time=_parse_metaapi_time(c["time"]),
                open=Decimal(str(c["open"])),
                high=Decimal(str(c["high"])),
                low=Decimal(str(c["low"])),
                close=Decimal(str(c["close"])),
                volume=Decimal(str(c["tickVolume"])) if "tickVolume" in c else None,
            )
            for c in raw_candles
            if end is None or _parse_metaapi_time(c["time"]) <= end
        ]
        return CandleSeries(source=self.source, symbol=symbol, timeframe=canonical_tf, candles=candles)

    async def get_positions(self, account_ref: str) -> list[Position]:
        data = await self._client_get(account_ref, "/positions")
        return [_position_from_raw(self.source, p) for p in (data or [])]

    async def get_orders(self, account_ref: str) -> list[Order]:
        data = await self._client_get(account_ref, "/orders")
        return [_order_from_raw(self.source, o) for o in (data or [])]

    async def get_history_orders(self, account_ref: str, start: datetime, end: datetime) -> list[Order]:
        path = f"/history-orders/time/{_to_metaapi_time(start)}/{_to_metaapi_time(end)}"
        data = await self._client_get(account_ref, path)
        orders = data.get("historyOrders", data) if isinstance(data, dict) else data
        return [_order_from_raw(self.source, o) for o in (orders or [])]

    async def get_history_deals(self, account_ref: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        path = f"/history-deals/time/{_to_metaapi_time(start)}/{_to_metaapi_time(end)}"
        data = await self._client_get(account_ref, path)
        deals = data.get("deals", data) if isinstance(data, dict) else data
        return list(deals or [])

    async def calculate_margin(
        self, account_ref: str, symbol: str, side: OrderSide, volume: Decimal, order_type: OrderType = OrderType.MARKET
    ) -> MarginEstimate:
        action_type_by_order_type = {
            OrderType.MARKET: _ACTION_TYPE_MARKET,
            OrderType.LIMIT: _ACTION_TYPE_LIMIT,
            OrderType.STOP: _ACTION_TYPE_STOP,
        }
        body = {
            "symbol": symbol,
            "type": action_type_by_order_type[order_type][side],
            "volume": float(volume),
        }
        data = await self._client_post(account_ref, "/calculate-margin", body)
        margin = data.get("margin")
        if margin is None:
            raise BrokerRequestError(f"MetaApi calculate-margin returned no margin for {symbol}: {data}")
        return MarginEstimate(
            source=self.source, symbol=symbol, volume=volume, margin=Decimal(str(margin)), currency=""
        )

    # ------------------------------------------------------------------
    # Execution — only ever invoked after human approval (execution_service.py)
    # ------------------------------------------------------------------

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
    ) -> BrokerOrderResult:
        if order_type == OrderType.MARKET:
            action_type = _ACTION_TYPE_MARKET[side]
        elif order_type == OrderType.LIMIT:
            action_type = _ACTION_TYPE_LIMIT[side]
        else:
            action_type = _ACTION_TYPE_STOP[side]

        body: dict[str, Any] = {"actionType": action_type, "symbol": symbol, "volume": float(volume)}
        if stop_loss is not None:
            body["stopLoss"] = float(stop_loss)
        if take_profit is not None:
            body["takeProfit"] = float(take_profit)
        if order_type != OrderType.MARKET:
            if limit_price is None:
                raise BrokerRequestError(f"{order_type} order requires a limit_price")
            body["openPrice"] = float(limit_price)

        data = await self._client_post(account_ref, "/trade", body)
        return _order_result_from_raw(self.source, data)

    async def modify_order(
        self,
        account_ref: str,
        order_id: str,
        stop_loss: Decimal | None = None,
        take_profit: Decimal | None = None,
        limit_price: Decimal | None = None,
    ) -> BrokerOrderResult:
        body: dict[str, Any] = {"actionType": "ORDER_MODIFY", "orderId": order_id}
        if stop_loss is not None:
            body["stopLoss"] = float(stop_loss)
        if take_profit is not None:
            body["takeProfit"] = float(take_profit)
        if limit_price is not None:
            body["openPrice"] = float(limit_price)
        data = await self._client_post(account_ref, "/trade", body)
        return _order_result_from_raw(self.source, data)

    async def cancel_order(self, account_ref: str, order_id: str) -> BrokerOrderResult:
        body = {"actionType": "ORDER_CANCEL", "orderId": order_id}
        data = await self._client_post(account_ref, "/trade", body)
        return _order_result_from_raw(self.source, data)

    async def modify_position(
        self, account_ref: str, position_id: str, stop_loss: Decimal | None = None, take_profit: Decimal | None = None
    ) -> BrokerOrderResult:
        body: dict[str, Any] = {"actionType": "POSITION_MODIFY", "positionId": position_id}
        if stop_loss is not None:
            body["stopLoss"] = float(stop_loss)
        if take_profit is not None:
            body["takeProfit"] = float(take_profit)
        data = await self._client_post(account_ref, "/trade", body)
        return _order_result_from_raw(self.source, data)

    async def close_position(self, account_ref: str, position_id: str) -> BrokerOrderResult:
        body = {"actionType": "POSITION_CLOSE_ID", "positionId": position_id}
        data = await self._client_post(account_ref, "/trade", body)
        return _order_result_from_raw(self.source, data)


def _position_from_raw(source: str, p: dict[str, Any]) -> Position:
    return Position(
        source=source,
        position_id=str(p.get("id")),
        symbol=p.get("symbol", ""),
        side=OrderSide.BUY if p.get("type") == "POSITION_TYPE_BUY" else OrderSide.SELL,
        volume=Decimal(str(p.get("volume", 0))),
        open_price=Decimal(str(p.get("openPrice", 0))),
        current_price=Decimal(str(p["currentPrice"])) if p.get("currentPrice") is not None else None,
        stop_loss=Decimal(str(p["stopLoss"])) if p.get("stopLoss") is not None else None,
        take_profit=Decimal(str(p["takeProfit"])) if p.get("takeProfit") is not None else None,
        profit=Decimal(str(p["profit"])) if p.get("profit") is not None else None,
        opened_at=_parse_metaapi_time(p["time"]) if p.get("time") else None,
        raw=p,
    )


def _order_from_raw(source: str, o: dict[str, Any]) -> Order:
    order_type_raw = o.get("type", "")
    side = OrderSide.BUY if "BUY" in order_type_raw else OrderSide.SELL
    if "LIMIT" in order_type_raw:
        order_type = OrderType.LIMIT
    elif "STOP" in order_type_raw:
        order_type = OrderType.STOP
    else:
        order_type = OrderType.MARKET
    return Order(
        source=source,
        order_id=str(o.get("id")),
        symbol=o.get("symbol", ""),
        side=side,
        order_type=order_type,
        volume=Decimal(str(o.get("volume", 0))),
        open_price=Decimal(str(o["openPrice"])) if o.get("openPrice") is not None else None,
        stop_loss=Decimal(str(o["stopLoss"])) if o.get("stopLoss") is not None else None,
        take_profit=Decimal(str(o["takeProfit"])) if o.get("takeProfit") is not None else None,
        state=o.get("state"),
        created_at=_parse_metaapi_time(o["time"]) if o.get("time") else None,
        raw=o,
    )


def _order_result_from_raw(source: str, data: dict[str, Any]) -> BrokerOrderResult:
    return BrokerOrderResult(
        source=source,
        broker_order_id=str(data["orderId"]) if data.get("orderId") is not None else None,
        position_id=str(data["positionId"]) if data.get("positionId") is not None else None,
        status=data.get("stringCode", data.get("tradeExecutionTime", "unknown")),
        price=Decimal(str(data["price"])) if data.get("price") is not None else None,
        raw=data,
    )


def _parse_metaapi_time(value: str | None) -> datetime:
    if not value:
        raise BrokerRequestError("MetaApi response is missing a required timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _to_metaapi_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
