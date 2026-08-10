"""
OANDA v20 REST adapter — the platform's market-data source.

Uses a single platform-owned OANDA account/API token (see
PlatformBrokerCredential, broker='oanda') to serve quotes/candles/instruments
to every tenant. No end user needs their own OANDA account or credentials.

This is a data source only — no trade execution is implemented here (see
§4.2 of the trading domain spec). All real-money execution goes through
MetaApiClient instead.

Reference: https://developer.oanda.com/rest-live-v20/introduction/
"""

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from src.services.trading.brokers.base import (
    BrokerAuthError,
    BrokerRequestError,
    BrokerTimeoutError,
    Candle,
    CandleSeries,
    MarketDataAdapter,
    Quote,
    SymbolInfo,
)
from src.services.trading.timeframes import validate_timeframe

logger = logging.getLogger(__name__)

_PRACTICE_BASE_URL = "https://api-fxpractice.oanda.com"
_LIVE_BASE_URL = "https://api-fxtrade.oanda.com"

# Canonical timeframe -> OANDA candlestick granularity. OANDA's own codes
# already match our M1/M5/../H4 convention; only day/week differ.
_TIMEFRAME_TO_GRANULARITY = {
    "M1": "M1",
    "M5": "M5",
    "M15": "M15",
    "M30": "M30",
    "H1": "H1",
    "H4": "H4",
    "D1": "D",
    "W1": "W",
}

_REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)


class OandaClient(MarketDataAdapter):
    """OANDA v20 market-data adapter. One instance per (token, environment, account_id)."""

    source = "oanda"

    def __init__(self, token: str, environment: str, account_id: str):
        if not token:
            raise BrokerAuthError("OANDA token is required")
        if environment not in ("practice", "live"):
            raise ValueError(f"Unsupported OANDA environment '{environment}' (expected practice|live)")
        self._token = token
        self._environment = environment
        self._account_id = account_id
        self._base_url = _PRACTICE_BASE_URL if environment == "practice" else _LIVE_BASE_URL

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept-Datetime-Format": "RFC3339"}

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(headers=self._headers(), timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.get(url, params=params)
        except httpx.TimeoutException as exc:
            raise BrokerTimeoutError(f"OANDA request timed out: {path}") from exc
        except httpx.HTTPError as exc:
            raise BrokerRequestError(f"OANDA request failed: {exc}") from exc

        if resp.status_code == 401:
            raise BrokerAuthError("OANDA rejected the platform credential (401)")
        if resp.status_code >= 400:
            raise BrokerRequestError(f"OANDA returned {resp.status_code} for {path}: {resp.text[:300]}")
        return resp.json()

    async def get_instruments(self) -> list[SymbolInfo]:
        data = await self._get(f"/v3/accounts/{self._account_id}/instruments")
        instruments = data.get("instruments", [])
        return [
            SymbolInfo(
                source=self.source,
                symbol=inst["name"],
                description=inst.get("displayName"),
                pip_size=Decimal(str(inst["pipLocation"])) if "pipLocation" in inst else None,
                volume_min=Decimal(str(inst["minimumTradeSize"])) if "minimumTradeSize" in inst else None,
                volume_max=Decimal(str(inst["maximumOrderUnits"])) if "maximumOrderUnits" in inst else None,
                tradeable=True,
                raw=inst,
            )
            for inst in instruments
        ]

    async def get_quote(self, symbol: str) -> Quote:
        data = await self._get(f"/v3/accounts/{self._account_id}/pricing", params={"instruments": symbol})
        prices = data.get("prices", [])
        if not prices:
            raise BrokerRequestError(f"OANDA returned no pricing for symbol '{symbol}'")
        price = prices[0]
        bids = price.get("bids") or []
        asks = price.get("asks") or []
        if not bids or not asks:
            raise BrokerRequestError(f"OANDA pricing for '{symbol}' has no bid/ask (market may be closed)")
        bid = Decimal(bids[0]["price"])
        ask = Decimal(asks[0]["price"])
        return Quote(
            source=self.source,
            symbol=symbol,
            bid=bid,
            ask=ask,
            spread=ask - bid,
            timestamp=_parse_oanda_time(price["time"]),
            tradeable=price.get("tradeable", True),
        )

    async def get_candles(
        self,
        symbol: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> CandleSeries:
        canonical_tf = validate_timeframe(timeframe)
        granularity = _TIMEFRAME_TO_GRANULARITY[canonical_tf]

        params: dict[str, Any] = {"granularity": granularity, "price": "M"}
        if start is not None:
            params["from"] = _to_oanda_time(start)
        if end is not None:
            params["to"] = _to_oanda_time(end)
        if limit is not None and start is None and end is None:
            # OANDA's `count` param is only valid when from/to aren't both given
            params["count"] = min(limit, 5000)

        data = await self._get(f"/v3/instruments/{symbol}/candles", params=params)
        candles = [
            Candle(
                time=_parse_oanda_time(c["time"]),
                open=Decimal(c["mid"]["o"]),
                high=Decimal(c["mid"]["h"]),
                low=Decimal(c["mid"]["l"]),
                close=Decimal(c["mid"]["c"]),
                volume=Decimal(str(c["volume"])) if "volume" in c else None,
            )
            for c in data.get("candles", [])
            if c.get("complete", True)
        ]
        if limit is not None:
            candles = candles[-limit:]
        return CandleSeries(source=self.source, symbol=symbol, timeframe=canonical_tf, candles=candles)

    async def get_account_summary(self) -> dict[str, Any]:
        """Summary of the platform's own OANDA account — never an end user's."""
        data = await self._get(f"/v3/accounts/{self._account_id}/summary")
        return data.get("account", {})


def _parse_oanda_time(value: str) -> datetime:
    # OANDA returns RFC3339 with up to 9 fractional digits; trim to microsecond precision.
    if "." in value:
        head, frac_and_tz = value.split(".", 1)
        frac = frac_and_tz.rstrip("Z")[:6]
        tz = "Z" if value.endswith("Z") else frac_and_tz[len(frac) :]
        value = f"{head}.{frac}{tz}"
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _to_oanda_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
