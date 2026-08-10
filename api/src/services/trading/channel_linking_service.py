"""
Channel identity linking — §16/§17 of the trading domain spec.

Maps a Telegram/WhatsApp identity to an authenticated Synkora Account so a
trade-approval action taken from that channel can be attributed to (and
permission-checked against) a real account, not just trusted at face value.
Ordinary chat with the trading agent never requires linking — only
approve/reject actions do.

Flow: an authenticated user generates a short-lived one-time code from the
web dashboard (`generate_link_code`), then sends it to the bot
(`/link <code>` on Telegram, a `LINK <code>` message on WhatsApp — no bot
command system there). The channel-side handler resolves the code
(`resolve_link_code`, single-use — deleted from Redis on read) and stores
the linked account id on that channel's Conversation row.
"""

import logging
import secrets
import uuid

from src.config.redis import get_redis_async

logger = logging.getLogger(__name__)

_LINK_CODE_TTL_SECONDS = 600  # 10 minutes
_REDIS_KEY_PREFIX = "trading_link_code"


def _redis_key(code: str) -> str:
    return f"{_REDIS_KEY_PREFIX}:{code}"


async def generate_link_code(tenant_id: uuid.UUID, account_id: uuid.UUID) -> str:
    """Generate a one-time code an authenticated user can send to a bot to link their identity."""
    code = secrets.token_hex(4).upper()  # 8 hex chars, e.g. "A1B2C3D4"
    redis = get_redis_async()
    await redis.set(_redis_key(code), f"{tenant_id}:{account_id}", ex=_LINK_CODE_TTL_SECONDS)
    return code


async def resolve_link_code(code: str) -> tuple[str, str] | None:
    """Resolve and consume a one-time link code. Returns (tenant_id, account_id) or None if invalid/expired."""
    redis = get_redis_async()
    key = _redis_key(code.strip().upper())
    value = await redis.get(key)
    if not value:
        return None
    await redis.delete(key)  # single-use
    value_str = value.decode() if isinstance(value, bytes) else value
    tenant_id, _, account_id = value_str.partition(":")
    if not tenant_id or not account_id:
        return None
    return tenant_id, account_id
