"""
Approval -> execution — §9.2/§9.3/§10/§F of the trading domain spec.

This is deliberately NOT wired through the platform's generic
HumanApprovalService.handle_reply()/respond_to_approval() (which re-fires
the *agent run* that made the original tool call — right for a generic
Slack/email side-effect, wrong for a financial order that needs its own
MetaApi price/margin revalidation and a dedicated broker call). Instead:
approve_proposal()/reject_proposal() here are the only way a TradeProposal
moves out of PENDING_APPROVAL, and they keep the linked AgentApprovalRequest
row in sync purely for the platform's existing notification/audit/expiry
bookkeeping — never for tool re-execution.

Authorization: every entry point here requires the distinct `trading:approve`
permission (§9.5) — chat access to the trading agent never implies it, and
the agent itself is never in the set of accounts that can hold it.

Duplicate execution prevention (§9.4/§F): a compare-and-swap UPDATE moves
PENDING_APPROVAL -> APPROVED and APPROVED -> EXECUTING; either transition
racing twice only ever succeeds once (rowcount check). The database's own
partial unique index (one EXECUTED row per proposal — see the Phase 1
migration) is the hard backstop under that.
"""

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.activity_log import ActivityLog, ActivityType
from src.models.agent_approval import ApprovalStatus
from src.models.trade_execution import TradeExecution
from src.models.trade_proposal import TradeProposal, TradeProposalStatus
from src.models.trading_account import TradingAccount
from src.models.trading_configuration import RiskConfiguration
from src.services.permissions.permission_service import PermissionService
from src.services.trading import risk_engine
from src.services.trading.broker_credential_service import get_metaapi_client
from src.services.trading.brokers.base import BrokerError, OrderSide, OrderType

logger = logging.getLogger(__name__)

# Reject execution outright if the MetaApi price at execution time has moved
# more than this fraction from the price captured at proposal time — a fresh
# proposal/approval is required instead of silently executing at a worse price.
_MAX_ACCEPTABLE_SLIPPAGE_FRACTION = Decimal("0.005")


class ApprovalAuthorizationError(Exception):
    """Raised when the acting account does not hold trading:approve."""


class ProposalStateError(Exception):
    """Raised when a proposal isn't in the expected state (already handled, expired, not found)."""


async def _require_approval_permission(db: AsyncSession, tenant_id: str, approver_account_id: str) -> None:
    allowed = await PermissionService(db).check_permission(
        uuid.UUID(str(approver_account_id)), uuid.UUID(str(tenant_id)), "trading", "approve"
    )
    if not allowed:
        raise ApprovalAuthorizationError(
            "This account does not hold the trading:approve permission. Ordinary chat access to the "
            "trading agent does not grant approval authority — a tenant admin must grant it explicitly."
        )


async def _load_proposal_for_tenant(db: AsyncSession, tenant_id: str, proposal_id: str) -> TradeProposal:
    proposal = (
        await db.execute(
            select(TradeProposal).where(
                TradeProposal.id == uuid.UUID(str(proposal_id)), TradeProposal.tenant_id == uuid.UUID(str(tenant_id))
            )
        )
    ).scalar_one_or_none()
    if proposal is None:
        raise ProposalStateError(f"Proposal {proposal_id} not found for this tenant.")
    return proposal


async def _maybe_expire(db: AsyncSession, proposal: TradeProposal) -> TradeProposal:
    if (
        proposal.status == TradeProposalStatus.PENDING_APPROVAL
        and proposal.expires_at is not None
        and proposal.expires_at < datetime.now(UTC)
    ):
        await _cas_update(db, proposal.id, TradeProposalStatus.PENDING_APPROVAL, TradeProposalStatus.EXPIRED)
        await _sync_approval_request(db, proposal, ApprovalStatus.EXPIRED)
        await db.commit()
        await db.refresh(proposal)
    return proposal


async def _cas_update(
    db: AsyncSession,
    proposal_id: uuid.UUID,
    expected_status: TradeProposalStatus,
    new_status: TradeProposalStatus,
    **extra,
) -> bool:
    """Compare-and-swap TradeProposal.status. Returns True iff this call performed the transition."""
    result = await db.execute(
        update(TradeProposal)
        .where(TradeProposal.id == proposal_id, TradeProposal.status == expected_status)
        .values(status=new_status, **extra)
    )
    return result.rowcount == 1


async def _sync_approval_request(db: AsyncSession, proposal: TradeProposal, status: ApprovalStatus) -> None:
    if not proposal.approval_request_id:
        return
    from src.models.agent_approval import AgentApprovalRequest

    approval = (
        await db.execute(select(AgentApprovalRequest).where(AgentApprovalRequest.id == proposal.approval_request_id))
    ).scalar_one_or_none()
    if approval is None:
        return
    approval.status = status
    approval.responded_at = datetime.now(UTC)


async def reject_proposal(
    db: AsyncSession, *, tenant_id: str, proposal_id: str, approver_account_id: str, reason: str | None = None
) -> TradeProposal:
    await _require_approval_permission(db, tenant_id, approver_account_id)
    proposal = await _maybe_expire(db, await _load_proposal_for_tenant(db, tenant_id, proposal_id))

    if proposal.status != TradeProposalStatus.PENDING_APPROVAL:
        raise ProposalStateError(f"Proposal {proposal_id} is '{proposal.status}', not pending approval.")

    transitioned = await _cas_update(
        db, proposal.id, TradeProposalStatus.PENDING_APPROVAL, TradeProposalStatus.REJECTED, failure_reason=reason
    )
    if not transitioned:
        raise ProposalStateError(f"Proposal {proposal_id} was already handled by a concurrent request.")

    await _sync_approval_request(db, proposal, ApprovalStatus.REJECTED)
    db.add(
        ActivityLog.log_activity(
            action="trading.proposal_rejected",
            activity_type=ActivityType.TRADING,
            account_id=approver_account_id,
            tenant_id=tenant_id,
            resource_type="trade_proposal",
            resource_id=str(proposal.id),
            description=f"Proposal {proposal.id} rejected by {approver_account_id}",
            activity_metadata={"reason": reason},
        )
    )
    await db.commit()
    await db.refresh(proposal)
    return proposal


async def approve_proposal(
    db: AsyncSession, *, tenant_id: str, proposal_id: str, approver_account_id: str
) -> TradeProposal:
    """Approve a proposal and immediately execute it (§F: APPROVED -> EXECUTING -> EXECUTED/FAILED)."""
    await _require_approval_permission(db, tenant_id, approver_account_id)
    proposal = await _maybe_expire(db, await _load_proposal_for_tenant(db, tenant_id, proposal_id))

    if proposal.status != TradeProposalStatus.PENDING_APPROVAL:
        raise ProposalStateError(f"Proposal {proposal_id} is '{proposal.status}', not pending approval.")

    transitioned = await _cas_update(
        db,
        proposal.id,
        TradeProposalStatus.PENDING_APPROVAL,
        TradeProposalStatus.APPROVED,
        approved_at=datetime.now(UTC),
        approved_by=uuid.UUID(str(approver_account_id)),
    )
    if not transitioned:
        raise ProposalStateError(f"Proposal {proposal_id} was already handled by a concurrent request.")

    await _sync_approval_request(db, proposal, ApprovalStatus.APPROVED)
    db.add(
        ActivityLog.log_activity(
            action="trading.approved",
            activity_type=ActivityType.TRADING,
            account_id=approver_account_id,
            tenant_id=tenant_id,
            resource_type="trade_proposal",
            resource_id=str(proposal.id),
            description=f"Proposal {proposal.id} approved by {approver_account_id}",
        )
    )
    await db.commit()
    await db.refresh(proposal)

    return await execute_proposal(db, tenant_id=tenant_id, proposal_id=str(proposal.id))


async def execute_proposal(db: AsyncSession, *, tenant_id: str, proposal_id: str) -> TradeProposal:
    proposal = await _load_proposal_for_tenant(db, tenant_id, proposal_id)
    if proposal.status != TradeProposalStatus.APPROVED:
        raise ProposalStateError(f"Proposal {proposal_id} is '{proposal.status}', not approved.")

    transitioned = await _cas_update(
        db,
        proposal.id,
        TradeProposalStatus.APPROVED,
        TradeProposalStatus.EXECUTING,
        execution_started_at=datetime.now(UTC),
    )
    if not transitioned:
        raise ProposalStateError(f"Proposal {proposal_id} execution already started by a concurrent request.")
    await db.commit()
    await db.refresh(proposal)

    trading_account = (
        await db.execute(select(TradingAccount).where(TradingAccount.id == proposal.trading_account_id))
    ).scalar_one()

    execution = TradeExecution(
        tenant_id=proposal.tenant_id,
        trade_proposal_id=proposal.id,
        status="executing",
        started_at=datetime.now(UTC),
    )
    db.add(execution)
    await db.flush()

    db.add(
        ActivityLog.log_activity(
            action="trading.execution_started",
            activity_type=ActivityType.TRADING,
            tenant_id=tenant_id,
            resource_type="trade_proposal",
            resource_id=str(proposal.id),
            description=f"Execution started for proposal {proposal.id}",
        )
    )
    await db.commit()

    try:
        await _do_execute(db, proposal, trading_account, execution)
    except Exception as exc:
        logger.error(f"Execution failed for proposal {proposal.id}: {exc}", exc_info=True)
        await _fail_execution(db, proposal, execution, str(exc))

    await db.refresh(proposal)
    return proposal


async def _do_execute(
    db: AsyncSession, proposal: TradeProposal, trading_account: TradingAccount, execution: TradeExecution
) -> None:
    client = await get_metaapi_client(db)

    # §10: re-fetch everything fresh from MetaApi — never reuse the OANDA or
    # proposal-time MetaApi numbers for the actual execution decision.
    try:
        account_info = await client.get_account_information(trading_account.metaapi_account_id)
        symbol_info = await client.get_symbol_information(trading_account.metaapi_account_id, proposal.symbol)
        quote = await client.get_quote(trading_account.metaapi_account_id, proposal.symbol)
        positions = await client.get_positions(trading_account.metaapi_account_id)
    except BrokerError as exc:
        await _fail_execution(db, proposal, execution, f"Could not revalidate against MetaApi: {exc}")
        return

    fresh_price = quote.ask if proposal.action == "buy" else quote.bid
    execution.revalidated_price = fresh_price

    if proposal.metaapi_price_at_proposal is not None:
        slippage = abs(fresh_price - proposal.metaapi_price_at_proposal)
        execution.slippage = slippage
        if slippage > proposal.metaapi_price_at_proposal * _MAX_ACCEPTABLE_SLIPPAGE_FRACTION:
            await _fail_execution(
                db,
                proposal,
                execution,
                f"Price moved from {proposal.metaapi_price_at_proposal} to {fresh_price} since the proposal "
                "was approved — conditions changed materially. A new proposal is required.",
            )
            return

    risk_config = (
        await db.execute(select(RiskConfiguration).where(RiskConfiguration.trading_account_id == trading_account.id))
    ).scalar_one_or_none()

    trade_request = risk_engine.TradeRequest(
        action=proposal.action,
        order_type=OrderType(proposal.order_type),
        symbol=proposal.symbol,
        volume=proposal.volume,
        stop_loss=proposal.stop_loss,
        take_profit=proposal.take_profit,
        limit_price=proposal.limit_price,
        reference_price=fresh_price,
    )
    limits = risk_engine.RiskLimits(
        max_position_size=risk_config.max_position_size if risk_config else None,
        max_risk_pct_per_trade=risk_config.max_risk_pct_per_trade if risk_config else None,
        max_open_positions=risk_config.max_open_positions if risk_config else None,
    )
    required_margin = None
    if proposal.action in ("buy", "sell"):
        try:
            side = OrderSide.BUY if proposal.action == "buy" else OrderSide.SELL
            margin_estimate = await client.calculate_margin(
                trading_account.metaapi_account_id,
                proposal.symbol,
                side,
                proposal.volume,
                OrderType(proposal.order_type),
            )
            required_margin = margin_estimate.margin
            execution.margin_at_execution = account_info.free_margin
        except BrokerError as exc:
            await _fail_execution(db, proposal, execution, f"Could not recalculate margin before execution: {exc}")
            return

    revalidation = risk_engine.validate_trade_request(
        trade_request,
        account_info,
        symbol_info,
        limits,
        open_position_count=len(positions),
        duplicate_pending_exists=False,  # this proposal IS the one executing
        required_margin=required_margin,
    )
    if not revalidation.passed:
        await _fail_execution(
            db, proposal, execution, "Re-validation before execution failed: " + "; ".join(revalidation.errors)
        )
        return

    try:
        broker_result = await _place_broker_order(client, trading_account, proposal)
    except BrokerError as exc:
        await _fail_execution(db, proposal, execution, f"Broker rejected the order: {exc}")
        return

    now = datetime.now(UTC)
    execution.status = "executed"
    execution.broker_order_id = broker_result.broker_order_id
    execution.result = broker_result.raw
    execution.completed_at = now

    proposal.status = TradeProposalStatus.EXECUTED
    proposal.execution_completed_at = now
    proposal.broker_order_id = broker_result.broker_order_id

    await _sync_approval_request_executed(db, proposal, broker_result.raw)

    db.add(
        ActivityLog.log_activity(
            action="trading.execution_succeeded",
            activity_type=ActivityType.TRADING,
            tenant_id=str(proposal.tenant_id),
            resource_type="trade_proposal",
            resource_id=str(proposal.id),
            description=f"Executed {proposal.action} {proposal.volume} {proposal.symbol}",
            activity_metadata={"broker_order_id": broker_result.broker_order_id, "status": broker_result.status},
        )
    )
    await db.commit()


async def _place_broker_order(client, trading_account: TradingAccount, proposal: TradeProposal):
    account_ref = trading_account.metaapi_account_id
    if proposal.action in ("buy", "sell"):
        side = OrderSide.BUY if proposal.action == "buy" else OrderSide.SELL
        return await client.create_order(
            account_ref,
            proposal.symbol,
            side,
            OrderType(proposal.order_type),
            proposal.volume,
            stop_loss=proposal.stop_loss,
            take_profit=proposal.take_profit,
            limit_price=proposal.limit_price,
        )
    if proposal.action == "close":
        return await client.close_position(account_ref, proposal.broker_order_id or "")
    if proposal.action == "modify":
        return await client.modify_position(
            account_ref, proposal.broker_order_id or "", proposal.stop_loss, proposal.take_profit
        )
    if proposal.action == "cancel":
        return await client.cancel_order(account_ref, proposal.broker_order_id or "")
    raise BrokerError(f"Unsupported proposal action '{proposal.action}'")


async def _fail_execution(db: AsyncSession, proposal: TradeProposal, execution: TradeExecution, reason: str) -> None:
    now = datetime.now(UTC)
    execution.status = "failed"
    execution.failure_reason = reason
    execution.completed_at = now

    proposal.status = TradeProposalStatus.FAILED
    proposal.failure_reason = reason
    proposal.execution_completed_at = now

    await _sync_approval_request(db, proposal, ApprovalStatus.FAILED)

    db.add(
        ActivityLog.log_activity(
            action="trading.execution_failed",
            activity_type=ActivityType.TRADING,
            tenant_id=str(proposal.tenant_id),
            resource_type="trade_proposal",
            resource_id=str(proposal.id),
            description=f"Execution failed for proposal {proposal.id}: {reason}",
            status="failure",
            error_message=reason,
        )
    )
    await db.commit()


async def _sync_approval_request_executed(db: AsyncSession, proposal: TradeProposal, execution_result: dict) -> None:
    if not proposal.approval_request_id:
        return
    from src.models.agent_approval import AgentApprovalRequest

    approval = (
        await db.execute(select(AgentApprovalRequest).where(AgentApprovalRequest.id == proposal.approval_request_id))
    ).scalar_one_or_none()
    if approval is None:
        return
    approval.status = ApprovalStatus.EXECUTED
    approval.execution_result = execution_result
