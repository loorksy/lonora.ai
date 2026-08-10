"""
Risk validation engine — runs in code, never relies on the agent's system
prompt (§11/RULE 9 of the trading domain spec).

Executed twice per proposal lifecycle:
  1. At proposal creation (proposal_service.py), against data fetched at
     that moment.
  2. Immediately before execution (execution_service.py), against data
     freshly re-fetched from MetaApi — never reusing the first pass's
     numbers, since conditions may have changed (§10).

Every function here is a pure function over plain data — no DB session, no
broker client, no network call. Callers (proposal_service.py /
execution_service.py) are responsible for fetching the inputs.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from src.services.trading.brokers.base import AccountInformation, OrderSide, OrderType, SymbolInfo


@dataclass
class RiskCheckResult:
    passed: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "errors": self.errors, "warnings": self.warnings}


@dataclass
class TradeRequest:
    action: str  # buy | sell | close | modify | cancel
    order_type: OrderType
    symbol: str
    volume: Decimal | None  # required for buy/sell; not meaningful for modify/cancel
    stop_loss: Decimal | None
    take_profit: Decimal | None
    limit_price: Decimal | None
    reference_price: Decimal


@dataclass
class RiskLimits:
    max_position_size: Decimal | None = None
    max_risk_pct_per_trade: Decimal | None = None
    max_open_positions: int | None = None
    max_daily_loss_pct: Decimal | None = None


_NEW_POSITION_ACTIONS = {"buy", "sell"}
_MIN_SL_TP_DISTANCE_FRACTION = Decimal("0.0001")  # 1 pip on a 4-decimal pair, floor sanity check


def validate_trade_request(
    request: TradeRequest,
    account: AccountInformation,
    symbol_info: SymbolInfo,
    limits: RiskLimits,
    open_position_count: int,
    duplicate_pending_exists: bool,
    required_margin: Decimal | None,
) -> RiskCheckResult:
    """Run every applicable check for a single proposed trade request.

    Returns a result with passed=False and the full list of failure
    reasons if anything is wrong — never partial/short-circuited, so a
    caller can show the user everything that needs fixing at once.
    """
    errors: list[str] = []
    warnings: list[str] = []

    is_new_position = request.action in _NEW_POSITION_ACTIONS

    # Account connectivity + trading permission
    if not account.trade_allowed:
        errors.append("This account does not currently permit trading (trade_allowed=false).")

    # Symbol validity / tradability
    if not symbol_info.tradeable:
        errors.append(f"Symbol '{request.symbol}' is not currently tradeable (market closed or disabled).")

    # Stop-loss required for any new position — never optional, never derived from the prompt
    if is_new_position and request.stop_loss is None:
        errors.append("A stop-loss is required for every new position and was not provided.")

    # Volume against the symbol's own constraints (not applicable to modify/cancel)
    if is_new_position or request.action == "close":
        if request.volume is None or request.volume <= 0:
            errors.append("Volume must be provided and greater than zero.")
        else:
            if symbol_info.volume_min is not None and request.volume < symbol_info.volume_min:
                errors.append(f"Volume {request.volume} is below the symbol minimum {symbol_info.volume_min}.")
            if symbol_info.volume_max is not None and request.volume > symbol_info.volume_max:
                errors.append(f"Volume {request.volume} exceeds the symbol maximum {symbol_info.volume_max}.")
            if symbol_info.volume_step is not None and symbol_info.volume_step > 0:
                remainder = (request.volume / symbol_info.volume_step) % 1
                if remainder not in (Decimal(0),):
                    errors.append(
                        f"Volume {request.volume} does not align to the symbol's volume step {symbol_info.volume_step}."
                    )

    # Order type / limit price consistency
    if request.order_type != OrderType.MARKET and request.limit_price is None:
        errors.append(f"order_type='{request.order_type}' requires a limit_price.")

    # SL/TP distance sanity (never zero-distance, never on the wrong side of price for the direction)
    if request.stop_loss is not None:
        distance = abs(request.reference_price - request.stop_loss)
        if distance == 0:
            errors.append("Stop-loss cannot equal the entry/reference price.")
        elif distance < request.reference_price * _MIN_SL_TP_DISTANCE_FRACTION:
            errors.append("Stop-loss is unrealistically close to the reference price.")
        elif is_new_position:
            side = OrderSide.BUY if request.action == "buy" else OrderSide.SELL
            if side == OrderSide.BUY and request.stop_loss >= request.reference_price:
                errors.append("Stop-loss must be below the reference price for a buy.")
            elif side == OrderSide.SELL and request.stop_loss <= request.reference_price:
                errors.append("Stop-loss must be above the reference price for a sell.")

    if request.take_profit is not None:
        distance = abs(request.reference_price - request.take_profit)
        if distance == 0:
            errors.append("Take-profit cannot equal the entry/reference price.")
        elif is_new_position:
            side = OrderSide.BUY if request.action == "buy" else OrderSide.SELL
            if side == OrderSide.BUY and request.take_profit <= request.reference_price:
                errors.append("Take-profit must be above the reference price for a buy.")
            elif side == OrderSide.SELL and request.take_profit >= request.reference_price:
                errors.append("Take-profit must be below the reference price for a sell.")

    # Margin sufficiency
    if required_margin is not None and required_margin > account.free_margin:
        errors.append(
            f"Insufficient free margin: requires ~{required_margin} {account.currency}, "
            f"available {account.free_margin} {account.currency}."
        )

    # Position-size / risk limits (RiskConfiguration)
    if (
        limits.max_position_size is not None
        and request.volume is not None
        and request.volume > limits.max_position_size
    ):
        errors.append(
            f"Volume {request.volume} exceeds the account's configured max position size {limits.max_position_size}."
        )

    if (
        limits.max_risk_pct_per_trade is not None
        and request.stop_loss is not None
        and request.volume is not None
        and account.equity > 0
    ):
        if symbol_info.contract_size is None:
            warnings.append(
                "Symbol contract size is unavailable — the estimated risk % below assumes contract_size=1 "
                "and may significantly understate real risk for standard FX lots. Treat it as approximate."
            )
        contract_size = symbol_info.contract_size or Decimal(1)
        risk_amount = abs(request.reference_price - request.stop_loss) * request.volume * contract_size
        risk_pct = (risk_amount / account.equity) * 100
        max_allowed = limits.max_risk_pct_per_trade
        if risk_pct > max_allowed:
            errors.append(
                f"Estimated risk {risk_pct:.2f}% of equity exceeds the account's configured max "
                f"{max_allowed}% per trade."
            )

    if limits.max_open_positions is not None and is_new_position and open_position_count >= limits.max_open_positions:
        errors.append(
            f"Account already has {open_position_count} open position(s), at or above the configured "
            f"maximum of {limits.max_open_positions}."
        )

    # Duplicate/conflicting proposal check
    if duplicate_pending_exists:
        errors.append(
            f"A pending proposal already exists for {request.symbol}/{request.action} on this account — "
            "resolve or let it expire before proposing another."
        )

    return RiskCheckResult(passed=not errors, errors=errors, warnings=warnings)
