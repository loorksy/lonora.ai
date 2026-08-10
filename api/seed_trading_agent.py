#!/usr/bin/env python3
"""
Script to create (or update) the specialized Trading Agent for a tenant.

This does not reinvent agent creation — it reuses the exact same
persistence shape and LLM-config resolution as the real
`POST /api/v1/agents` endpoint (src/controllers/agents/index.py), just
driven from the CLI instead of HTTP, so the seeded agent is guaranteed to
look like one a human created through the normal Agent Builder.

Usage:
    python seed_trading_agent.py --tenant-id <uuid> [--api-key sk-...] [--provider anthropic] [--model claude-...]

If --api-key is omitted, the agent inherits the tenant's Platform Engineer
agent's LLM config (same fallback the real endpoint uses) — the script
fails clearly if neither is available, rather than creating a broken agent.

Re-running this script for the same tenant updates the existing Trading
Agent's system prompt, description, and tool list in place instead of
creating a duplicate.
"""

import argparse
import asyncio
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from sqlalchemy import select  # noqa: E402

from src.controllers.agents.index import _resolve_primary_llm_config, _resolve_requested_agent_tools  # noqa: E402
from src.core.database import get_async_session_factory  # noqa: E402
from src.models.agent import Agent  # noqa: E402
from src.models.agent_llm_config import AgentLLMConfig  # noqa: E402
from src.models.agent_tool import AgentTool  # noqa: E402
from src.services.agents.trading_agent_prompt import (  # noqa: E402
    TRADING_AGENT_DESCRIPTION,
    TRADING_AGENT_SYSTEM_PROMPT,
)

AGENT_NAME = "Trading Agent"


class _LLMConfigArgs:
    """Duck-types the subset of the CreateAgentRequest.config.llm_config schema that
    _resolve_primary_llm_config actually reads — avoids depending on the full pydantic
    request schema for a CLI-driven seed script."""

    def __init__(self, provider: str | None, model_name: str | None, api_key: str | None):
        self.provider = provider
        self.model_name = model_name
        self.api_key = api_key
        self.api_base = None
        self.temperature = 0.4
        self.max_tokens = None
        self.top_p = None
        self.additional_params = {}


def _build_slug(name: str) -> str:
    base_slug = re.sub(r"[^a-z0-9-]", "-", name.lower())
    return re.sub(r"-+", "-", base_slug).strip("-") or "agent"


async def seed_trading_agent(
    tenant_id: uuid.UUID,
    created_by: uuid.UUID,
    provider: str | None,
    model_name: str | None,
    api_key: str | None,
) -> None:
    factory = get_async_session_factory()
    async with factory() as db:
        existing = (
            await db.execute(select(Agent).where(Agent.tenant_id == tenant_id, Agent.agent_name == AGENT_NAME))
        ).scalar_one_or_none()

        matched_tool_names = _resolve_requested_agent_tools(["trading_tools"])
        if not matched_tool_names:
            print("WARNING: no tools matched the 'trading_tools' category — is trading_tools_registry registered?")

        if existing is not None:
            print(f"Trading Agent already exists for tenant {tenant_id} (id={existing.id}) — updating in place.")
            existing.system_prompt = TRADING_AGENT_SYSTEM_PROMPT
            existing.description = TRADING_AGENT_DESCRIPTION
            existing.category = "Trading"
            existing.agent_metadata = {**(existing.agent_metadata or {}), "domain": "trading"}

            current_tool_names = {
                t.tool_name
                for t in (await db.execute(select(AgentTool).where(AgentTool.agent_id == existing.id))).scalars()
            }
            for tool_name in matched_tool_names:
                if tool_name not in current_tool_names:
                    db.add(AgentTool(agent_id=existing.id, tool_name=tool_name, config={}, enabled=True))

            await db.commit()
            print(f"Updated Trading Agent {existing.id} — {len(matched_tool_names)} trading tools ensured attached.")
            return

        effective_llm_config = await _resolve_primary_llm_config(
            db=db,
            tenant_id=tenant_id,
            agent_name=AGENT_NAME,
            llm_config=_LLMConfigArgs(provider, model_name, api_key),
        )

        db_agent = Agent(
            tenant_id=tenant_id,
            created_by=created_by,
            agent_name=AGENT_NAME,
            agent_type="llm",
            description=TRADING_AGENT_DESCRIPTION,
            system_prompt=TRADING_AGENT_SYSTEM_PROMPT,
            llm_config={
                "provider": effective_llm_config.provider,
                "model_name": effective_llm_config.model_name,
            },
            agent_metadata={"domain": "trading"},
            status="ACTIVE",
            category="Trading",
            tags=["trading", "forex", "finance"],
        )
        db.add(db_agent)
        db_agent.slug = _build_slug(AGENT_NAME)
        await db.flush()

        for tool_name in matched_tool_names:
            db.add(AgentTool(agent_id=db_agent.id, tool_name=tool_name, config={}, enabled=True))

        db.add(
            AgentLLMConfig(
                tenant_id=tenant_id,
                agent_id=db_agent.id,
                name=f"Primary {effective_llm_config.model_name}",
                provider=effective_llm_config.provider,
                model_name=effective_llm_config.model_name,
                api_key=effective_llm_config.encrypted_api_key,
                api_base=effective_llm_config.api_base,
                temperature=effective_llm_config.temperature,
                max_tokens=effective_llm_config.max_tokens,
                top_p=effective_llm_config.top_p,
                additional_params=effective_llm_config.additional_params,
                is_default=True,
                display_order=0,
                enabled=True,
            )
        )
        await db.commit()
        print(f"Created Trading Agent {db_agent.id} for tenant {tenant_id} — {len(matched_tool_names)} tools attached.")
        print("Next: attach a TradingAccount-linking flow and, once Phase 5 lands, the propose_* execution tools.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or update the Trading Agent for a tenant.")
    parser.add_argument("--tenant-id", required=True, help="Tenant UUID to create the Trading Agent under")
    parser.add_argument("--created-by", help="Account UUID to record as creator (defaults to tenant-id if omitted)")
    parser.add_argument("--provider", help="LLM provider, e.g. anthropic (omit to inherit Platform Engineer config)")
    parser.add_argument("--model", dest="model_name", help="Model name, e.g. claude-sonnet-5")
    parser.add_argument("--api-key", help="LLM API key (omit to inherit Platform Engineer config)")
    args = parser.parse_args()

    tenant_id = uuid.UUID(args.tenant_id)
    created_by = uuid.UUID(args.created_by) if args.created_by else tenant_id

    asyncio.run(seed_trading_agent(tenant_id, created_by, args.provider, args.model_name, args.api_key))


if __name__ == "__main__":
    main()
