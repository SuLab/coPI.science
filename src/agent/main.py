"""Agent simulation engine entry point.

Usage:
    python -m src.agent.main --max-runtime 60 --budget 50
"""

import asyncio
import logging
import signal
import sys
import uuid
from datetime import datetime, timezone

import typer

from src.agent.agent import Agent
from src.agent.channels import SEEDED_CHANNELS
from src.agent.simulation import PILOT_LABS, SimulationEngine
from src.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = typer.Typer()


@app.command()
def main(
    max_runtime: int = typer.Option(60, "--max-runtime", help="Max runtime in minutes"),
    budget: int = typer.Option(50, "--budget", help="Max LLM calls per agent"),
    mock: bool = typer.Option(False, "--mock", help="Run in mock mode without real Slack tokens"),
    no_db: bool = typer.Option(False, "--no-db", help="Skip database logging"),
):
    """Run the agent simulation."""
    asyncio.run(_run_simulation(max_runtime, budget, mock, no_db))


async def _run_simulation(
    max_runtime: int,
    budget: int,
    mock: bool,
    no_db: bool,
) -> None:
    settings = get_settings()

    # Create agent instances
    agents = [
        Agent(agent_id=lab["id"], bot_name=lab["name"], pi_name=lab["pi"])
        for lab in PILOT_LABS
    ]

    # Set up Slack clients
    slack_clients = {}
    if not mock:
        from src.agent.slack_client import AgentSlackClient
        slack_tokens = settings.get_slack_tokens()
        for agent in agents:
            tokens = slack_tokens.get(agent.agent_id, {})
            bot_token = tokens.get("bot", "")
            app_token = tokens.get("app", "")
            if bot_token and not bot_token.startswith("xoxb-placeholder"):
                client = AgentSlackClient(
                    agent_id=agent.agent_id,
                    bot_token=bot_token,
                    app_token=app_token,
                    on_message=lambda msg: None,  # Will be replaced by simulation engine
                )
                slack_clients[agent.agent_id] = client
                client.start()
            else:
                logger.warning("[%s] No valid Slack token — skipping Slack connection", agent.agent_id)

    # Set up database session factory
    session_factory = None
    simulation_run_id = None

    if not no_db:
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from src.models import SimulationRun
        engine = create_async_engine(settings.database_url)
        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        # Create simulation run record
        async with session_factory() as db:
            run = SimulationRun(
                status="running",
                config={
                    "max_runtime": max_runtime,
                    "budget_cap": budget,
                    "mock": mock,
                    "agent_count": len(agents),
                },
            )
            db.add(run)
            await db.commit()
            simulation_run_id = run.id
            logger.info("Created simulation run %s", simulation_run_id)

    # Create simulation engine
    engine = SimulationEngine(
        agents=agents,
        slack_clients=slack_clients,
        max_runtime_minutes=max_runtime,
        budget_cap=budget,
        session_factory=session_factory,
        simulation_run_id=simulation_run_id,
    )

    # Handle shutdown signals
    loop = asyncio.get_event_loop()

    def shutdown():
        logger.info("Received shutdown signal")
        asyncio.ensure_future(engine.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown)

    try:
        logger.info(
            "Starting simulation: %d agents, %dm max runtime, %d budget/agent",
            len(agents), max_runtime, budget,
        )
        await engine.start()
    finally:
        # Update simulation run status
        if session_factory and simulation_run_id:
            async with session_factory() as db:
                from sqlalchemy import select
                from src.models import SimulationRun
                result = await db.execute(
                    select(SimulationRun).where(SimulationRun.id == simulation_run_id)
                )
                run = result.scalar_one_or_none()
                if run:
                    run.status = "completed"
                    run.ended_at = datetime.now(timezone.utc)
                    run.total_api_calls = sum(a.api_call_count for a in agents)
                    await db.commit()

        # Stop Slack clients
        for client in slack_clients.values():
            client.stop()

        # Update working memories
        if session_factory and simulation_run_id:
            logger.info("Updating agent working memories...")
            await engine.update_all_working_memories()

        logger.info("Simulation complete.")
        logger.info(
            "Summary: %s",
            {a.agent_id: {"messages": a.message_count, "api_calls": a.api_call_count}
             for a in agents},
        )


if __name__ == "__main__":
    app()
