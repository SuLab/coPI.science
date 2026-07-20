"""One-time backfill: import current Slack conversation history into the DB.

Since the DB became the primary conversation store (specs/local-db-conversations.md),
agent_messages carries message content. Historically content lived only in Slack,
so pre-cutover runs have metadata-only rows. Run this once, with Slack tokens
available, to pull the workspace's channel + thread history into agent_messages
(content + slack_ts as the canonical message_ts) before switching to DB-primary
operation, preserving in-flight conversations.

It reuses the engine's own setup/rebuild machinery (seeded + private channels,
the Slack reconcile, and the persist flush), then exits — it does NOT run any
agent turns or make LLM calls.

Idempotent: the reconcile appends only messages not already in the DB, and the
flush upserts on (simulation_run_id, message_ts). Safe to re-run.

Usage (inside the app container):

    docker exec copi-python-opus-app-1 python scripts/backfill_slack_history_to_db.py
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.config import get_settings
from src.models import AgentMessage, AgentRegistry, SimulationRun
from src.services.slack_tokens import env_token, is_valid_token

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("backfill_slack_history")


async def main(run_id_arg: str | None) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Roster (active agents) + tokens, mirroring src/agent/main.py.
    async with sf() as db:
        rows = (await db.execute(
            select(
                AgentRegistry.agent_id, AgentRegistry.bot_name,
                AgentRegistry.pi_name, AgentRegistry.slack_bot_token,
            ).where(AgentRegistry.status == "active").order_by(AgentRegistry.agent_id)
        )).all()
        if run_id_arg:
            run_id = run_id_arg
        else:
            run_id = (await db.execute(
                select(SimulationRun.id).order_by(desc(SimulationRun.started_at)).limit(1)
            )).scalar_one_or_none()

    if run_id is None:
        logger.error("No SimulationRun found — start a run first (nothing to attach to).")
        await engine.dispose()
        return

    agents = [Agent(agent_id=r.agent_id, bot_name=r.bot_name, pi_name=r.pi_name) for r in rows]

    from src.agent.slack_client import AgentSlackClient
    slack_clients = {}
    for r in rows:
        tok = r.slack_bot_token if is_valid_token(r.slack_bot_token) else env_token(r.agent_id)
        if is_valid_token(tok):
            client = AgentSlackClient(agent_id=r.agent_id, bot_token=tok)
            if client.connect():
                slack_clients[r.agent_id] = client
    if not slack_clients:
        logger.error("No connected Slack clients — cannot backfill from Slack.")
        await engine.dispose()
        return

    async with sf() as db:
        before = (await db.execute(
            select(func.count(AgentMessage.id)).where(
                AgentMessage.simulation_run_id == run_id,
                func.length(AgentMessage.content) > 0,
            )
        )).scalar_one()

    sim = SimulationEngine(
        agents=agents, slack_clients=slack_clients, session_factory=sf,
        simulation_run_id=run_id, slack_enabled=True,
    )
    # Reuse the engine's setup + rebuild, then flush to the DB. No turns run.
    sim._ensure_seeded_channels()
    await sim._persist_seeded_channels()
    await sim._sync_private_channels_from_db()
    sim.message_log.set_persist_callback(sim._enqueue_persist)
    await sim._rebuild_state_from_db()
    await sim._rebuild_state_from_slack()
    await sim._flush_persisted()

    async with sf() as db:
        after = (await db.execute(
            select(func.count(AgentMessage.id)).where(
                AgentMessage.simulation_run_id == run_id,
                func.length(AgentMessage.content) > 0,
            )
        )).scalar_one()

    logger.info(
        "Backfill complete for run %s: content-bearing messages %d -> %d (log holds %d).",
        run_id, before, after, len(sim.message_log),
    )
    await engine.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", default=None, help="Target SimulationRun id (default: latest)")
    args = ap.parse_args()
    asyncio.run(main(args.run_id))
