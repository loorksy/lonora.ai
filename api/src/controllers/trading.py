"""
Trading Controller — REST API for the Forex/trading domain (§22 of the
trading domain spec).

Endpoints follow the platform's existing conventions: `/api/v1/trading/...`
prefix, `Depends(get_current_tenant_id)`/`Depends(get_current_account)` for
auth, `{"success": bool, "data"/"error": ...}` response shape (matching
mcp_servers.py and friends). Tenant isolation is enforced explicitly on
every query — there is no platform-wide automatic filter to rely on.

Approval endpoints require `trading:approve` — enforced inside
execution_service.py, not just here, so there is no way to reach the
broker without that permission regardless of which entry point is used
(REST today; Telegram/WhatsApp callbacks in a later phase call the same
execution_service functions).
"""

import contextlib
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_async_db
from src.middleware.auth_middleware import get_current_account, get_current_tenant_id
from src.models.tenant import Account
from src.models.trade_proposal import TradeProposal, TradeProposalStatus
from src.models.trading_account import TradingAccount
from src.services.trading import channel_linking_service, execution_service, market_data_service
from src.services.trading.broker_credential_service import get_metaapi_client
from src.services.trading.brokers.base import BrokerError

router = APIRouter(prefix="/api/v1/trading", tags=["trading"])


def _err(exc: Exception, status_code: int = 400) -> HTTPException:
    return HTTPException(status_code=status_code, detail=str(exc))


class LinkTradingAccountRequest(BaseModel):
    login: str
    password: str
    server: str
    label: str | None = None


class ApproveRejectRequest(BaseModel):
    reason: str | None = None


# ---------------------------------------------------------------------------
# Channel identity linking (§16/§17)
# ---------------------------------------------------------------------------


@router.post("/channel-link/generate")
async def generate_channel_link_code(
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    current_account: Account = Depends(get_current_account),
):
    """Generate a short-lived one-time code the caller sends to a Telegram/WhatsApp bot
    (`/link <code>` or `LINK <code>`) to link that channel identity to their account for
    trade-approval purposes. Valid for 10 minutes, single-use."""
    code = await channel_linking_service.generate_link_code(tenant_id, current_account.id)
    return {"success": True, "data": {"code": code, "expires_in_seconds": 600}}


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


@router.post("/accounts")
async def link_trading_account(
    body: LinkTradingAccountRequest,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    current_account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_async_db),
):
    """Link a MetaApi (MT4/MT5) account. Only ever required for execution — never for market data/analysis."""
    try:
        client = await get_metaapi_client(db)
        metaapi_account_id = await client.link_account(
            login=body.login, password=body.password, server=body.server, name=body.label or body.login
        )
    except BrokerError as exc:
        raise _err(exc, 502) from exc

    account = TradingAccount(
        tenant_id=tenant_id,
        account_id=current_account.id,
        broker="metaapi",
        metaapi_account_id=metaapi_account_id,
        mt_login=body.login,
        mt_server=body.server,
        label=body.label,
        status="pending",
    )
    account.mt_password = body.password
    db.add(account)
    await db.commit()
    await db.refresh(account)

    # Deployment can be retried later; the link itself already succeeded.
    with contextlib.suppress(BrokerError):
        await client.deploy_account(metaapi_account_id)

    return {"success": True, "data": account.to_dict()}


@router.get("/accounts")
async def list_trading_accounts(
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    current_account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_async_db),
):
    result = await db.execute(
        select(TradingAccount).where(
            TradingAccount.tenant_id == tenant_id, TradingAccount.account_id == current_account.id
        )
    )
    accounts = result.scalars().all()
    return {"success": True, "data": [a.to_dict() for a in accounts]}


@router.delete("/accounts/{account_id}")
async def unlink_trading_account(
    account_id: str,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    current_account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_async_db),
):
    result = await db.execute(
        select(TradingAccount).where(
            TradingAccount.id == uuid.UUID(account_id),
            TradingAccount.tenant_id == tenant_id,
            TradingAccount.account_id == current_account.id,
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=404, detail="Trading account not found")

    if account.metaapi_account_id:
        # Proceed with the local unlink even if the remote cleanup fails.
        with contextlib.suppress(BrokerError):
            client = await get_metaapi_client(db)
            await client.remove_account(account.metaapi_account_id)

    await db.delete(account)
    await db.commit()
    return {"success": True}


@router.get("/accounts/{account_id}/positions")
async def get_account_positions(
    account_id: str,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    current_account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_async_db),
):
    try:
        result = await market_data_service.get_positions(
            db, str(tenant_id), account_id, requesting_account_id=str(current_account.id)
        )
        return {"success": True, "data": result}
    except BrokerError as exc:
        raise _err(exc, 502) from exc
    except ValueError as exc:
        raise _err(exc, 404) from exc


@router.get("/accounts/{account_id}/orders")
async def get_account_orders(
    account_id: str,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    current_account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_async_db),
):
    try:
        result = await market_data_service.get_orders(
            db, str(tenant_id), account_id, requesting_account_id=str(current_account.id)
        )
        return {"success": True, "data": result}
    except BrokerError as exc:
        raise _err(exc, 502) from exc
    except ValueError as exc:
        raise _err(exc, 404) from exc


@router.get("/accounts/{account_id}/history")
async def get_account_history(
    account_id: str,
    start: datetime,
    end: datetime,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    current_account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_async_db),
):
    try:
        result = await market_data_service.get_trade_history(
            db, str(tenant_id), account_id, start, end, requesting_account_id=str(current_account.id)
        )
        return {"success": True, "data": result}
    except BrokerError as exc:
        raise _err(exc, 502) from exc
    except ValueError as exc:
        raise _err(exc, 404) from exc


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


@router.get("/market/quote")
async def get_market_quote(
    source: str,
    symbol: str,
    trading_account_id: str | None = None,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    current_account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_async_db),
):
    try:
        result = await market_data_service.get_quote(
            db,
            str(tenant_id),
            source,
            symbol,
            trading_account_id=trading_account_id,
            requesting_account_id=str(current_account.id),
        )
        return {"success": True, "data": result}
    except (BrokerError, ValueError) as exc:
        raise _err(exc, 502) from exc


@router.get("/market/candles")
async def get_market_candles(
    source: str,
    symbol: str,
    timeframe: str,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int | None = None,
    trading_account_id: str | None = None,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    current_account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_async_db),
):
    try:
        result = await market_data_service.get_candles(
            db,
            str(tenant_id),
            source,
            symbol,
            timeframe,
            start=start,
            end=end,
            limit=limit,
            trading_account_id=trading_account_id,
            requesting_account_id=str(current_account.id),
        )
        return {"success": True, "data": result}
    except (BrokerError, ValueError) as exc:
        raise _err(exc, 502) from exc


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------


@router.get("/proposals")
async def list_proposals(
    status: str | None = None,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    current_account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_async_db),
):
    stmt = select(TradeProposal).where(
        TradeProposal.tenant_id == tenant_id, TradeProposal.account_id == current_account.id
    )
    if status:
        stmt = stmt.where(TradeProposal.status == TradeProposalStatus(status))
    result = await db.execute(stmt.order_by(TradeProposal.created_at.desc()))
    proposals = result.scalars().all()
    return {"success": True, "data": [p.to_dict() for p in proposals]}


@router.get("/proposals/{proposal_id}")
async def get_proposal(
    proposal_id: str,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_async_db),
):
    result = await db.execute(
        select(TradeProposal).where(TradeProposal.id == uuid.UUID(proposal_id), TradeProposal.tenant_id == tenant_id)
    )
    proposal = result.scalar_one_or_none()
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    return {"success": True, "data": proposal.to_dict()}


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal_endpoint(
    proposal_id: str,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    current_account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_async_db),
):
    try:
        proposal = await execution_service.approve_proposal(
            db, tenant_id=str(tenant_id), proposal_id=proposal_id, approver_account_id=str(current_account.id)
        )
        return {"success": True, "data": proposal.to_dict()}
    except execution_service.ApprovalAuthorizationError as exc:
        raise _err(exc, 403) from exc
    except execution_service.ProposalStateError as exc:
        raise _err(exc, 409) from exc


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal_endpoint(
    proposal_id: str,
    body: ApproveRejectRequest,
    tenant_id: uuid.UUID = Depends(get_current_tenant_id),
    current_account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_async_db),
):
    try:
        proposal = await execution_service.reject_proposal(
            db,
            tenant_id=str(tenant_id),
            proposal_id=proposal_id,
            approver_account_id=str(current_account.id),
            reason=body.reason,
        )
        return {"success": True, "data": proposal.to_dict()}
    except execution_service.ApprovalAuthorizationError as exc:
        raise _err(exc, 403) from exc
    except execution_service.ProposalStateError as exc:
        raise _err(exc, 409) from exc
