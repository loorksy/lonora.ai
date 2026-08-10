"""
Trading execution tools — the ONLY trading tools that touch real money, and
even these never execute anything directly (§9/RULE 8 of the trading domain
spec). Every function here does exactly one thing: validate + create a
PENDING_APPROVAL TradeProposal and notify a human. None of them call a
broker. Execution only ever happens through execution_service.py, after an
authenticated human holding trading:approve approves the proposal via the
API (or, in a later phase, Telegram/WhatsApp buttons).

Registered with tool_category="action" so the platform's existing HITL
approval-gate gating (adk_tools.py::_check_approval_gate) also covers them
as a second layer — though the primary gate for trading proposals is the
dedicated one built here (create_proposal already creates its own
AgentApprovalRequest), this keeps them consistent with how every other
action-category tool in the platform is tagged for observability.
"""

import logging
import uuid
from decimal import Decimal
from typing import Any

from src.core.database import get_async_db
from src.services.trading import proposal_service
from src.services.trading.proposal_service import ProposalRequest, ProposalValidationError

logger = logging.getLogger(__name__)


def _get_field(runtime_context: Any, key: str) -> Any:
    if isinstance(runtime_context, dict):
        return runtime_context.get(key)
    return getattr(runtime_context, key, None)


def _context(runtime_context: Any) -> tuple[str | None, str | None, str | None, str | None]:
    """Extract (tenant_id, user_id, agent_id, conversation_id) from a RuntimeContext object or dict."""
    if runtime_context is None:
        return None, None, None, None
    tenant_id = _get_field(runtime_context, "tenant_id")
    user_id = _get_field(runtime_context, "user_id")
    agent_id = _get_field(runtime_context, "agent_id")
    conversation_id = _get_field(runtime_context, "conversation_id")
    return (
        str(tenant_id) if tenant_id else None,
        str(user_id) if user_id else None,
        str(agent_id) if agent_id else None,
        str(conversation_id) if conversation_id else None,
    )


async def _resolve_notification_channel(db: Any, conversation_id: str | None) -> tuple[str, dict]:
    """Detect which channel this conversation is running on so the approval notification lands
    there (Telegram inline buttons, WhatsApp interactive buttons) instead of the generic in-chat
    HITL prompt. Falls back to "chat" when the conversation isn't linked to any bot conversation."""
    if not conversation_id:
        return "chat", {}

    from sqlalchemy import select

    from src.models.telegram_bot import TelegramConversation
    from src.models.whatsapp_bot import WhatsAppBot, WhatsAppConversation

    conv_uuid = uuid.UUID(conversation_id)

    result = await db.execute(select(TelegramConversation).where(TelegramConversation.conversation_id == conv_uuid))
    tg_conv = result.scalar_one_or_none()
    if tg_conv is not None:
        return "telegram", {"bot_id": str(tg_conv.telegram_bot_id), "chat_id": tg_conv.telegram_chat_id}

    result = await db.execute(select(WhatsAppConversation).where(WhatsAppConversation.conversation_id == conv_uuid))
    wa_conv = result.scalar_one_or_none()
    if wa_conv is not None:
        wa_bot = await db.get(WhatsAppBot, wa_conv.whatsapp_bot_id)
        if wa_bot is not None and wa_bot.connection_type == "cloud_api":
            return "whatsapp", {"bot_id": str(wa_conv.whatsapp_bot_id), "to_phone": wa_conv.whatsapp_user_id}
        # device_link (QR) bots run on WhatsAppDeviceLinkManager, which has no message-send entry
        # point outside its own connection thread — the platform's existing "whatsapp_web"
        # notification channel only works during an active QR-linking session, not for a
        # long-running connected bot. Fall back to the in-chat HITL prompt rather than silently
        # failing to deliver a trade approval notification.
        return "chat", {}

    return "chat", {}


async def _propose(request: ProposalRequest, runtime_context: Any, agent_name: str) -> dict[str, Any]:
    tenant_id, user_id, agent_id, conversation_id = _context(runtime_context)
    if not tenant_id or not user_id or not agent_id:
        return {"success": False, "error": "Missing tenant_id/user_id/agent_id in runtime context"}
    request.conversation_id = conversation_id

    try:
        async for db in get_async_db():
            request.notification_channel, request.channel_config = await _resolve_notification_channel(
                db, conversation_id
            )
            proposal = await proposal_service.create_proposal(
                db, tenant_id=tenant_id, account_id=user_id, agent_id=agent_id, agent_name=agent_name, request=request
            )
            return {
                "success": True,
                "proposal_id": str(proposal.id),
                "status": proposal.status.value,
                "symbol": proposal.symbol,
                "action": proposal.action,
                "volume": str(proposal.volume) if proposal.volume is not None else None,
                "volume_source": proposal.volume_source,
                "stop_loss": str(proposal.stop_loss) if proposal.stop_loss else None,
                "take_profit": str(proposal.take_profit) if proposal.take_profit else None,
                "expires_at": proposal.expires_at.isoformat() if proposal.expires_at else None,
                "message": (
                    "Proposal created and a human approval request has been sent. This is NOT executed yet — "
                    "nothing happens until an authorized human approves it."
                ),
            }
    except ProposalValidationError as exc:
        return {"success": False, "error": "Proposal rejected at validation", "reasons": exc.errors}
    except Exception as exc:
        logger.warning(f"Error creating trade proposal: {exc}", exc_info=True)
        return {"success": False, "error": str(exc)}


async def internal_trading_propose_market_order(
    trading_account_id: str,
    symbol: str,
    side: str,
    stop_loss: str,
    take_profit: str | None = None,
    volume: str | None = None,
    runtime_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Propose a market order (buy/sell). Creates a PENDING approval — never executes directly."""
    request = ProposalRequest(
        trading_account_id=trading_account_id,
        symbol=symbol,
        action=side,
        order_type="market",
        volume=Decimal(volume) if volume else None,
        stop_loss=Decimal(stop_loss),
        take_profit=Decimal(take_profit) if take_profit else None,
    )
    return await _propose(request, runtime_context, "Trading Agent")


async def internal_trading_propose_limit_order(
    trading_account_id: str,
    symbol: str,
    side: str,
    limit_price: str,
    stop_loss: str,
    take_profit: str | None = None,
    volume: str | None = None,
    runtime_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Propose a limit order (buy/sell at a specific price or better). Creates a PENDING approval."""
    request = ProposalRequest(
        trading_account_id=trading_account_id,
        symbol=symbol,
        action=side,
        order_type="limit",
        volume=Decimal(volume) if volume else None,
        stop_loss=Decimal(stop_loss),
        take_profit=Decimal(take_profit) if take_profit else None,
        limit_price=Decimal(limit_price),
    )
    return await _propose(request, runtime_context, "Trading Agent")


async def internal_trading_propose_stop_order(
    trading_account_id: str,
    symbol: str,
    side: str,
    stop_price: str,
    stop_loss: str,
    take_profit: str | None = None,
    volume: str | None = None,
    runtime_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Propose a stop order (buy/sell triggered once price reaches stop_price). Creates a PENDING approval."""
    request = ProposalRequest(
        trading_account_id=trading_account_id,
        symbol=symbol,
        action=side,
        order_type="stop",
        volume=Decimal(volume) if volume else None,
        stop_loss=Decimal(stop_loss),
        take_profit=Decimal(take_profit) if take_profit else None,
        limit_price=Decimal(stop_price),
    )
    return await _propose(request, runtime_context, "Trading Agent")


async def internal_trading_propose_close_position(
    trading_account_id: str,
    position_id: str,
    volume: str,
    symbol: str,
    runtime_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Propose closing an existing open position (fully or partially). Creates a PENDING approval."""
    request = ProposalRequest(
        trading_account_id=trading_account_id,
        symbol=symbol,
        action="close",
        target_id=position_id,
        volume=Decimal(volume),
    )
    return await _propose(request, runtime_context, "Trading Agent")


async def internal_trading_propose_modify_position(
    trading_account_id: str,
    position_id: str,
    symbol: str,
    stop_loss: str | None = None,
    take_profit: str | None = None,
    runtime_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Propose changing the stop-loss/take-profit on an existing open position. Creates a PENDING approval."""
    if not stop_loss and not take_profit:
        return {"success": False, "error": "At least one of stop_loss/take_profit must be provided."}
    request = ProposalRequest(
        trading_account_id=trading_account_id,
        symbol=symbol,
        action="modify",
        target_id=position_id,
        stop_loss=Decimal(stop_loss) if stop_loss else None,
        take_profit=Decimal(take_profit) if take_profit else None,
    )
    return await _propose(request, runtime_context, "Trading Agent")


async def internal_trading_propose_cancel_order(
    trading_account_id: str,
    order_id: str,
    symbol: str,
    runtime_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Propose cancelling an existing pending order. Creates a PENDING approval."""
    request = ProposalRequest(
        trading_account_id=trading_account_id,
        symbol=symbol,
        action="cancel",
        target_id=order_id,
    )
    return await _propose(request, runtime_context, "Trading Agent")


async def internal_trading_get_pending_proposals(
    trading_account_id: str | None = None, runtime_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """List the caller's own pending trade proposals (awaiting approval)."""
    tenant_id, user_id, _agent_id, _conversation_id = _context(runtime_context)
    if not tenant_id:
        return {"success": False, "error": "Missing tenant_id in runtime context"}

    from sqlalchemy import select

    from src.models.trade_proposal import TradeProposal, TradeProposalStatus

    try:
        async for db in get_async_db():
            stmt = select(TradeProposal).where(
                TradeProposal.tenant_id == uuid.UUID(tenant_id),
                TradeProposal.status == TradeProposalStatus.PENDING_APPROVAL,
            )
            if user_id:
                stmt = stmt.where(TradeProposal.account_id == uuid.UUID(user_id))
            if trading_account_id:
                stmt = stmt.where(TradeProposal.trading_account_id == uuid.UUID(trading_account_id))
            result = await db.execute(stmt)
            proposals = result.scalars().all()
            return {
                "success": True,
                "proposals": [
                    {
                        "proposal_id": str(p.id),
                        "symbol": p.symbol,
                        "action": p.action,
                        "volume": str(p.volume) if p.volume is not None else None,
                        "stop_loss": str(p.stop_loss) if p.stop_loss else None,
                        "take_profit": str(p.take_profit) if p.take_profit else None,
                        "expires_at": p.expires_at.isoformat() if p.expires_at else None,
                    }
                    for p in proposals
                ],
            }
    except Exception as exc:
        logger.warning(f"Error listing pending proposals: {exc}", exc_info=True)
        return {"success": False, "error": str(exc)}
