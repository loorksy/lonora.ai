"""
Explicit OANDA <-> MetaApi symbol normalization.

OANDA and MetaApi (MT4/MT5) are genuinely different venues that can quote
different prices for the same instrument, and they do not share a symbol
naming convention: OANDA uses underscore-separated codes ('EUR_USD',
'XAU_USD'), MT4/MT5 brokers concatenate them ('EURUSD', 'XAUUSD') and often
append a broker-specific suffix ('EURUSD.a', 'EURUSDm', 'EURUSD_i', ...).

Per the trading domain spec, symbols must never be treated as interchangeable
by naive string manipulation alone. This module is the single place that
maps between the two conventions:

  - A static table covers the common majors/crosses/metals — this is the
    authoritative source and should be extended as new instruments are
    onboarded.
  - A generic fallback (strip the underscore) is used for anything not in
    the table, but is clearly flagged as a heuristic — it does NOT account
    for broker-specific MT suffixes. Callers that need a specific broker's
    exact tradeable symbol (e.g. before sending an order) MUST verify the
    mapped symbol against that account's MetaApi get_symbols()/
    get_symbol_information() result rather than trusting the fallback
    blindly — see market_data_service.py / proposal_service.py.
"""

from dataclasses import dataclass

# OANDA symbol -> MetaApi (generic, no broker suffix) symbol.
# Extend this table as new instruments are supported.
_OANDA_TO_METAAPI: dict[str, str] = {
    "EUR_USD": "EURUSD",
    "GBP_USD": "GBPUSD",
    "USD_JPY": "USDJPY",
    "USD_CHF": "USDCHF",
    "USD_CAD": "USDCAD",
    "AUD_USD": "AUDUSD",
    "NZD_USD": "NZDUSD",
    "EUR_GBP": "EURGBP",
    "EUR_JPY": "EURJPY",
    "EUR_CHF": "EURCHF",
    "GBP_JPY": "GBPJPY",
    "AUD_JPY": "AUDJPY",
    "XAU_USD": "XAUUSD",
    "XAG_USD": "XAGUSD",
    "WTICO_USD": "USOIL",
    "BCO_USD": "UKOIL",
}
_METAAPI_TO_OANDA: dict[str, str] = {v: k for k, v in _OANDA_TO_METAAPI.items()}


@dataclass
class SymbolMappingResult:
    symbol: str
    is_explicit: bool  # False if produced by the heuristic fallback — verify before trading


def oanda_to_metaapi(oanda_symbol: str) -> SymbolMappingResult:
    """Map an OANDA instrument code to its generic MetaApi/MT symbol."""
    if oanda_symbol in _OANDA_TO_METAAPI:
        return SymbolMappingResult(symbol=_OANDA_TO_METAAPI[oanda_symbol], is_explicit=True)
    heuristic = oanda_symbol.replace("_", "")
    return SymbolMappingResult(symbol=heuristic, is_explicit=False)


def metaapi_to_oanda(metaapi_symbol: str) -> SymbolMappingResult:
    """Map a MetaApi/MT symbol (broker suffix already stripped by the caller) to its OANDA instrument code."""
    if metaapi_symbol in _METAAPI_TO_OANDA:
        return SymbolMappingResult(symbol=_METAAPI_TO_OANDA[metaapi_symbol], is_explicit=True)
    if len(metaapi_symbol) == 6 and metaapi_symbol.isalpha():
        heuristic = f"{metaapi_symbol[:3]}_{metaapi_symbol[3:]}".upper()
        return SymbolMappingResult(symbol=heuristic, is_explicit=False)
    return SymbolMappingResult(symbol=metaapi_symbol, is_explicit=False)


def strip_broker_suffix(mt_symbol: str, known_symbols: list[str]) -> str:
    """Resolve a broker-suffixed MT symbol (e.g. 'EURUSD.a') to its base form when unambiguous.

    Only used when matching a generic symbol against a specific account's
    actual tradeable symbol list (from MetaApiClient.get_symbols) — never
    guesses when the base form isn't uniquely present in `known_symbols`.
    """
    if mt_symbol in known_symbols:
        return mt_symbol
    candidates = [s for s in known_symbols if s.startswith(mt_symbol) or s == mt_symbol]
    if len(candidates) == 1:
        return candidates[0]
    return mt_symbol
