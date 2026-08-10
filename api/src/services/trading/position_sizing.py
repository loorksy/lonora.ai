"""
Position sizing — §11.1 of the trading domain spec.

Volume/quantity is never left implicit or purely agent-decided: either the
agent/user supplies an explicit volume (still validated below), or volume
is derived server-side from the account's RiskConfiguration (max % of
equity risked per trade, divided by the stop-loss distance and the
instrument's contract size). Whichever path is used, the method is always
recorded on the resulting TradeProposal (volume_source: 'explicit' |
'risk_derived') for audit purposes — never silently.

The computed volume is always validated against the broker's own
instrument constraints (volume_min/volume_max/volume_step) before a
proposal is allowed to reach PENDING_APPROVAL — this module raises
PositionSizingError with a clear reason rather than silently clamping or
rounding up (rounding up would silently increase risk beyond what was
requested/computed).
"""

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from src.services.trading.brokers.base import SymbolInfo


class PositionSizingError(Exception):
    """Raised when a volume can't be determined or doesn't fit the symbol's constraints."""


@dataclass
class PositionSizeResult:
    volume: Decimal
    source: str  # 'explicit' | 'risk_derived'


def compute_volume(
    *,
    explicit_volume: Decimal | None,
    method: str,
    account_equity: Decimal,
    reference_price: Decimal,
    stop_loss: Decimal,
    symbol_info: SymbolInfo,
    max_risk_pct_per_trade: Decimal | None,
) -> PositionSizeResult:
    """Determine the order volume, then validate it against the symbol's own constraints.

    Args:
        explicit_volume: volume supplied by the agent/user (required when method='explicit')
        method: 'explicit' | 'risk_derived'
        account_equity: current account equity (for risk_derived sizing)
        reference_price: current/entry price
        stop_loss: proposed stop-loss price (required for risk_derived sizing — always
            required for new positions anyway per risk_engine.py)
        symbol_info: the broker's own symbol specification (volume_min/max/step, contract_size)
        max_risk_pct_per_trade: from the account's RiskConfiguration (required for risk_derived)
    """
    if method == "explicit":
        if explicit_volume is None or explicit_volume <= 0:
            raise PositionSizingError("volume_source='explicit' requires a positive explicit volume.")
        # Explicit volumes are validated as given, never silently rounded — see _validate_against_symbol below.
        volume = explicit_volume
        _validate_against_symbol(volume, symbol_info)
        return PositionSizeResult(volume=volume, source=method)
    elif method == "risk_derived":
        if max_risk_pct_per_trade is None or max_risk_pct_per_trade <= 0:
            raise PositionSizingError(
                "volume_source='risk_derived' requires the account's RiskConfiguration.max_risk_pct_per_trade to be set."
            )
        if symbol_info.contract_size is None:
            raise PositionSizingError(
                f"Cannot derive volume from risk % — no contract size available for symbol '{symbol_info.symbol}'."
            )
        sl_distance = abs(reference_price - stop_loss)
        if sl_distance == 0:
            raise PositionSizingError("Cannot derive volume from risk % — stop-loss distance is zero.")

        risk_amount = account_equity * (max_risk_pct_per_trade / Decimal(100))
        volume = risk_amount / (sl_distance * symbol_info.contract_size)
    else:
        raise PositionSizingError(f"Unknown position sizing method '{method}' (expected explicit|risk_derived).")

    volume = _snap_to_step(volume, symbol_info.volume_step)
    _validate_against_symbol(volume, symbol_info)

    return PositionSizeResult(volume=volume, source=method)


def _snap_to_step(volume: Decimal, volume_step: Decimal | None) -> Decimal:
    """Round DOWN to the nearest valid volume step — never up, which would silently increase risk."""
    if not volume_step or volume_step <= 0:
        return volume
    steps = (volume / volume_step).to_integral_value(rounding=ROUND_DOWN)
    return steps * volume_step


def _validate_against_symbol(volume: Decimal, symbol_info: SymbolInfo) -> None:
    if volume <= 0:
        raise PositionSizingError("Computed volume is zero or negative after rounding to the symbol's volume step.")
    if symbol_info.volume_min is not None and volume < symbol_info.volume_min:
        raise PositionSizingError(
            f"Computed volume {volume} is below the symbol's minimum volume {symbol_info.volume_min}."
        )
    if symbol_info.volume_max is not None and volume > symbol_info.volume_max:
        raise PositionSizingError(
            f"Computed volume {volume} exceeds the symbol's maximum volume {symbol_info.volume_max}."
        )
    if symbol_info.volume_step is not None and symbol_info.volume_step > 0:
        remainder = (volume / symbol_info.volume_step) % 1
        if remainder != 0 and not math.isclose(float(remainder), 0.0, abs_tol=1e-9):
            raise PositionSizingError(
                f"Computed volume {volume} does not align to the symbol's volume step {symbol_info.volume_step}."
            )
