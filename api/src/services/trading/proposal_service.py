"""
Trade proposal creation — §9.1 of the trading domain spec.

propose_market_order() and friends (the only execution-adjacent tools the
agent has, see internal_tools/trading_execution_tools.py) call into
create_proposal() here. This function NEVER contacts the broker to execute
anything — it only validates, sizes, and (if valid) creates a PENDING
proposal + notifies a human. Validation happens before any TradeProposal
row is persisted: a request that fails risk checks never becomes a stored
proposal, it just returns an error to the caller.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.activity_log import ActivityLog, ActivityType
from src.models.trade_proposal import TradeProposal, TradeProposalStatus
from src.models.trading_configuration import RiskConfiguration
from src.services.human_approval_service import HumanApprovalService
from src.services.trading import position_sizing, risk_engine
from src.services.trading.broker_credential_service import get_metaapi_client, get_trading_account_or_raise
from src.services.trading.brokers.base import BrokerError, OrderSide, OrderType

logger = logging.getLogger(__name__)


class ProposalValidationError(Exception):
    """Raised when a trade request fails validation — no TradeProposal is ever created for these."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass
class ProposalRequest:
    trading_account_id: str
    symbol: str
    action: str  # buy | sell | close | modify | cancel
    order_type: str = "market"
    volume: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    limit_price: Decimal | None = None
    # Existing MetaApi position/order id being acted on — required for close/modify/cancel,
    # unused for buy/sell (which create a new position instead).
    target_id: str | None = None
    oanda_reference_price: Decimal | None = None
    oanda_reference_price_at: datetime | None = None
    conversation_id: str | None = None
    notification_channel: str = "chat"
    timeout_minutes: int = 60


_DEFAULT_TIMEOUT_MINUTES = 60


async def create_proposal(
    db: AsyncSession,
    *,
    tenant_id: str,
    account_id: str,
    agent_id: str,
    agent_name: str,
    request: ProposalRequest,
) -> TradeProposal:
    account = await get_trading_account_or_raise(db, tenant_id, request.trading_account_id)
    if str(account.account_id) != str(account_id):
        raise ProposalValidationError(["This trading account does not belong to the requesting user."])
    if not account.metaapi_account_id:
        raise ProposalValidationError([f"Trading account {request.trading_account_id} is not fully connected yet."])

    risk_config = (
        await db.execute(select(RiskConfiguration).where(RiskConfiguration.trading_account_id == account.id))
    ).scalar_one_or_none()

    sizing_method = risk_config.position_sizing_method if risk_config else "explicit"
    max_risk_pct = risk_config.max_risk_pct_per_trade if risk_config else None
    max_position_size = risk_config.max_position_size if risk_config else None
    max_open_positions = risk_config.max_open_positions if risk_config else None

    client = await get_metaapi_client(db)
    try:
        account_info = await client.get_account_information(account.metaapi_account_id)
        symbol_info = await client.get_symbol_information(account.metaapi_account_id, request.symbol)
        quote = await client.get_quote(account.metaapi_account_id, request.symbol)
        positions = await client.get_positions(account.metaapi_account_id)
    except BrokerError as exc:
        raise ProposalValidationError([f"Could not validate against MetaApi: {exc}"]) from exc

    reference_price = quote.ask if request.action == "buy" else quote.bid

    is_new_position = request.action in ("buy", "sell")
    if request.action in ("close", "modify", "cancel") and not request.target_id:
        raise ProposalValidationError(
            [f"target_id (existing position/order id) is required for action='{request.action}'"]
        )

    volume = request.volume
    volume_source = "explicit"
    if is_new_position:
        if request.stop_loss is None:
            raise ProposalValidationError(["A stop-loss is required for every new position."])
        try:
            sized = position_sizing.compute_volume(
                explicit_volume=request.volume,
                method=sizing_method,
                account_equity=account_info.equity,
                reference_price=reference_price,
                stop_loss=request.stop_loss,
                symbol_info=symbol_info,
                max_risk_pct_per_trade=max_risk_pct,
            )
        except position_sizing.PositionSizingError as exc:
            raise ProposalValidationError([str(exc)]) from exc
        volume, volume_source = sized.volume, sized.source
    elif request.action == "close":
        if volume is None:
            raise ProposalValidationError(["volume is required for action='close'"])
    else:  # modify | cancel — volume is not meaningful
        volume, volume_source = None, "n/a"

    duplicate_exists = (
        await db.execute(
            select(TradeProposal).where(
                TradeProposal.trading_account_id == account.id,
                TradeProposal.symbol == request.symbol,
                TradeProposal.action == request.action,
                TradeProposal.status.in_(
                    [TradeProposalStatus.PROPOSED, TradeProposalStatus.PENDING_APPROVAL, TradeProposalStatus.APPROVED]
                ),
            )
        )
    ).scalar_one_or_none() is not None

    required_margin = None
    if is_new_position:
        try:
            side = OrderSide.BUY if request.action == "buy" else OrderSide.SELL
            order_type_enum = OrderType(request.order_type)
            margin_estimate = await client.calculate_margin(
                account.metaapi_account_id, request.symbol, side, volume, order_type_enum
            )
            required_margin = margin_estimate.margin
        except BrokerError as exc:
            raise ProposalValidationError([f"Could not calculate required margin: {exc}"]) from exc

    trade_request = risk_engine.TradeRequest(
        action=request.action,
        order_type=OrderType(request.order_type),
        symbol=request.symbol,
        volume=volume,
        stop_loss=request.stop_loss,
        take_profit=request.take_profit,
        limit_price=request.limit_price,
        reference_price=reference_price,
    )
    limits = risk_engine.RiskLimits(
        max_position_size=max_position_size,
        max_risk_pct_per_trade=max_risk_pct,
        max_open_positions=max_open_positions,
    )
    result = risk_engine.validate_trade_request(
        trade_request,
        account_info,
        symbol_info,
        limits,
        open_position_count=len(positions),
        duplicate_pending_exists=duplicate_exists,
        required_margin=required_margin,
    )

    if not result.passed:
        logger.info(f"Trade proposal rejected at validation for account {request.trading_account_id}: {result.errors}")
        ActivityLog.log_activity(
            action="trading.proposal_rejected_validation",
            activity_type=ActivityType.TRADING,
            account_id=account_id,
            tenant_id=tenant_id,
            resource_type="trading_account",
            resource_id=str(account.id),
            description=f"Proposal for {request.symbol}/{request.action} rejected at validation",
            activity_metadata={"errors": result.errors, "symbol": request.symbol, "action": request.action},
            status="failure",
        )
        raise ProposalValidationError(result.errors)

    proposal = TradeProposal(
        tenant_id=uuid.UUID(str(tenant_id)),
        account_id=uuid.UUID(str(account_id)),
        agent_id=uuid.UUID(str(agent_id)),
        trading_account_id=account.id,
        status=TradeProposalStatus.PROPOSED,
        broker="metaapi",
        symbol=request.symbol,
        action=request.action,
        order_type=request.order_type,
        volume=volume,
        volume_source=volume_source,
        stop_loss=request.stop_loss,
        take_profit=request.take_profit,
        limit_price=request.limit_price,
        oanda_reference_price=request.oanda_reference_price,
        oanda_reference_price_at=request.oanda_reference_price_at,
        metaapi_price_at_proposal=reference_price,
        risk_check_result=result.to_dict(),
        # For close/modify/cancel this is the EXISTING target id, set upfront — see
        # TradeProposal.broker_order_id's dual-purpose docstring.
        broker_order_id=request.target_id if not is_new_position else None,
    )
    db.add(proposal)
    await db.flush()

    db.add(
        ActivityLog.log_activity(
            action="trading.proposal_created",
            activity_type=ActivityType.TRADING,
            account_id=account_id,
            tenant_id=tenant_id,
            resource_type="trade_proposal",
            resource_id=str(proposal.id),
            description=f"Proposed {request.action} {volume} {request.symbol}",
            activity_metadata={
                "symbol": request.symbol,
                "action": request.action,
                "volume": str(volume),
                "volume_source": volume_source,
                "stop_loss": str(request.stop_loss) if request.stop_loss else None,
                "take_profit": str(request.take_profit) if request.take_profit else None,
            },
        )
    )

    timeout_minutes = request.timeout_minutes or _DEFAULT_TIMEOUT_MINUTES
    approval_service = HumanApprovalService(db)
    channel_config = {"conversation_id": request.conversation_id} if request.conversation_id else {}
    approval_result = await approval_service.create_and_notify(
        task_id=None,
        agent_id=uuid.UUID(str(agent_id)),
        tenant_id=uuid.UUID(str(tenant_id)),
        agent_name=agent_name,
        tool_name=f"propose_{request.action}_order",
        tool_args={
            "trading_account_id": request.trading_account_id,
            "symbol": request.symbol,
            "action": request.action,
            "volume": str(volume),
            "stop_loss": str(request.stop_loss) if request.stop_loss else None,
            "take_profit": str(request.take_profit) if request.take_profit else None,
            "proposal_id": str(proposal.id),
        },
        channel=request.notification_channel,
        channel_config=channel_config,
        timeout_minutes=timeout_minutes,
        conversation_id=uuid.UUID(request.conversation_id) if request.conversation_id else None,
    )

    proposal.approval_request_id = uuid.UUID(approval_result["approval_id"])
    proposal.status = TradeProposalStatus.PENDING_APPROVAL
    proposal.expires_at = datetime.fromisoformat(approval_result["expires_at"])

    db.add(
        ActivityLog.log_activity(
            action="trading.approval_requested",
            activity_type=ActivityType.TRADING,
            account_id=account_id,
            tenant_id=tenant_id,
            resource_type="trade_proposal",
            resource_id=str(proposal.id),
            description=f"Approval requested for proposal {proposal.id} via {request.notification_channel}",
        )
    )

    await db.commit()
    await db.refresh(proposal)
    return proposal
