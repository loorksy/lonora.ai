#!/usr/bin/env python3
"""
Script to create (or update) the single platform-owned broker credentials
the trading domain needs — one OANDA token (market data, shared by every
tenant) and one MetaApi token (execution, shared by every tenant; per-user
state lives on TradingAccount instead). See
src/models/platform_broker_credential.py for why these are platform-scoped
rather than per-tenant.

Without this script (or an equivalent manual insert), the trading domain
has no way to reach either broker in a fresh deployment — this closes that
gap; nothing else in the codebase creates these rows.

Usage (reads from environment variables — see api/.env.example):
    python seed_platform_broker_credentials.py [--update]

Re-running without --update leaves existing rows untouched and reports
what already exists; --update overwrites the token/account_ref on rows
that already exist for a given (broker, environment).
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from sqlalchemy import select  # noqa: E402

from src.core.database import get_async_session_factory  # noqa: E402
from src.models.platform_broker_credential import PlatformBrokerCredential  # noqa: E402


async def _seed_one(
    db,
    *,
    broker: str,
    environment: str,
    token: str | None,
    account_ref: str | None,
    update_existing: bool,
) -> tuple[bool, str]:
    if not token:
        return True, f"{broker}/{environment} skipped (no token in environment)"

    existing = (
        await db.execute(
            select(PlatformBrokerCredential).where(
                PlatformBrokerCredential.broker == broker,
                PlatformBrokerCredential.environment == environment,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        if not update_existing:
            return False, f"{broker}/{environment} already exists (id={existing.id}). Use --update to overwrite."
        existing.token = token
        existing.account_ref = account_ref
        existing.is_active = True
        await db.commit()
        return True, f"{broker}/{environment} updated (id={existing.id})"

    credential = PlatformBrokerCredential(broker=broker, environment=environment, is_active=True)
    credential.token = token
    credential.account_ref = account_ref
    db.add(credential)
    await db.commit()
    await db.refresh(credential)
    return True, f"{broker}/{environment} created (id={credential.id})"


async def seed_platform_broker_credentials(update_existing: bool) -> bool:
    factory = get_async_session_factory()
    async with factory() as db:
        results = [
            await _seed_one(
                db,
                broker="oanda",
                environment=os.getenv("OANDA_ENVIRONMENT", "practice"),
                token=os.getenv("OANDA_TOKEN"),
                account_ref=os.getenv("OANDA_ACCOUNT_ID"),
                update_existing=update_existing,
            ),
            await _seed_one(
                db,
                broker="metaapi",
                environment="production",
                token=os.getenv("METAAPI_TOKEN"),
                account_ref=None,
                update_existing=update_existing,
            ),
        ]

    all_ok = True
    for success, message in results:
        print(("OK: " if success else "SKIPPED: ") + message)
        all_ok = all_ok and success
    return all_ok


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Seed platform-owned OANDA/MetaApi broker credentials.")
    parser.add_argument("--update", action="store_true", help="Overwrite existing credential rows")
    args = parser.parse_args()

    if not os.getenv("OANDA_TOKEN") and not os.getenv("METAAPI_TOKEN"):
        print("Nothing to seed: set OANDA_TOKEN and/or METAAPI_TOKEN in the environment first (see .env.example).")
        sys.exit(1)

    ok = asyncio.run(seed_platform_broker_credentials(args.update))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
