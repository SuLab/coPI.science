"""Agent simulation engine entry point.

Usage:
    python -m src.agent.main                        # resume, run until stopped
    python -m src.agent.main --max-runtime 60       # resume, stop after 60 min
    python -m src.agent.main --fresh                 # wipe + fresh start
    python -m src.agent.main --fresh --max-runtime 60
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone

import typer

from src.agent.agent import Agent
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
    max_runtime: int = typer.Option(0, "--max-runtime", help="Max runtime in minutes (0 = run until stopped)"),
    budget: int = typer.Option(50, "--budget", help="Max LLM calls per agent"),
    mock: bool = typer.Option(False, "--mock", help="Run in mock mode without real Slack tokens"),
    no_db: bool = typer.Option(False, "--no-db", help="Skip database logging"),
    fresh: bool = typer.Option(False, "--fresh", help="Wipe simulation data and start fresh"),
    reset_cursors: bool = typer.Option(False, "--reset-cursors", help="Reset scan cursors so agents re-read all posts"),
):
    """Run the turn-based agent simulation."""
    asyncio.run(_run_simulation(max_runtime, budget, mock, no_db, fresh, reset_cursors))


async def _run_simulation(
    max_runtime: int,
    budget: int,
    mock: bool,
    no_db: bool,
    fresh: bool,
    reset_cursors: bool = False,
) -> None:
    settings = get_settings()

    # Create agent instances
    agents = [
        Agent(agent_id=lab["id"], bot_name=lab["name"], pi_name=lab["pi"])
        for lab in PILOT_LABS
    ]

    # Set up Slack clients (Web API only, no Socket Mode)
    slack_clients = {}
    if not mock:
        from src.agent.slack_client import AgentSlackClient
        slack_tokens = settings.get_slack_tokens()
        for agent in agents:
            tokens = slack_tokens.get(agent.agent_id, {})
            bot_token = tokens.get("bot", "")
            if bot_token and not bot_token.startswith("xoxb-placeholder"):
                client = AgentSlackClient(
                    agent_id=agent.agent_id,
                    bot_token=bot_token,
                )
                if client.connect():
                    slack_clients[agent.agent_id] = client
                else:
                    logger.warning("[%s] Slack connection failed — skipping", agent.agent_id)
            else:
                logger.warning("[%s] No valid Slack token — skipping", agent.agent_id)

    # Set up database session factory
    session_factory = None
    simulation_run_id = None

    if not no_db:
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from src.models import AgentChannel, AgentMessage, SimulationRun
        engine = create_async_engine(settings.database_url)
        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        if fresh:
            # Wipe simulation data for a clean start
            # Preserve thread_decisions and proposal_reviews (PI-facing review data)
            logger.info("--fresh: wiping simulation data (preserving proposals and reviews)...")
            async with session_factory() as db:
                await db.execute(AgentMessage.__table__.delete())
                await db.execute(AgentChannel.__table__.delete())
                await db.commit()
            logger.info("Simulation data wiped.")

            # Create new simulation run
            async with session_factory() as db:
                run = SimulationRun(
                    status="running",
                    config={
                        "max_runtime": max_runtime,
                        "budget_cap": budget,
                        "mock": mock,
                        "agent_count": len(agents),
                        "active_thread_threshold": settings.active_thread_threshold,
                        "max_thread_messages": settings.max_thread_messages,
                    },
                )
                db.add(run)
                await db.commit()
                simulation_run_id = run.id
                logger.info("Created new simulation run %s", simulation_run_id)
        else:
            # Resume: find the latest simulation run
            async with session_factory() as db:
                result = await db.execute(
                    select(SimulationRun)
                    .order_by(SimulationRun.started_at.desc())
                    .limit(1)
                )
                existing_run = result.scalar_one_or_none()

                if existing_run:
                    simulation_run_id = existing_run.id
                    existing_run.status = "running"
                    existing_run.ended_at = None
                    await db.commit()
                    logger.info("Resuming simulation run %s", simulation_run_id)
                else:
                    # No existing run — create one
                    run = SimulationRun(
                        status="running",
                        config={
                            "max_runtime": max_runtime,
                            "budget_cap": budget,
                            "mock": mock,
                            "agent_count": len(agents),
                            "active_thread_threshold": settings.active_thread_threshold,
                            "max_thread_messages": settings.max_thread_messages,
                        },
                    )
                    db.add(run)
                    await db.commit()
                    simulation_run_id = run.id
                    logger.info("Created new simulation run %s", simulation_run_id)

    # Create simulation engine
    runtime_label = f"{max_runtime}m" if max_runtime > 0 else "indefinite"
    sim_engine = SimulationEngine(
        agents=agents,
        slack_clients=slack_clients,
        max_runtime_minutes=max_runtime,
        budget_cap=budget,
        session_factory=session_factory,
        simulation_run_id=simulation_run_id,
        reset_cursors=reset_cursors,
    )

    # Handle shutdown signals
    loop = asyncio.get_event_loop()

    def shutdown():
        logger.info("Received shutdown signal")
        asyncio.ensure_future(sim_engine.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown)

    try:
        logger.info(
            "Starting simulation: %d agents, %s max runtime, %d budget/agent%s",
            len(agents), runtime_label, budget,
            " (fresh start)" if fresh else " (resuming)",
        )
        await sim_engine.start()
    except Exception:
        logger.exception("Simulation engine raised an exception")
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
                    run.status = "stopped"
                    run.ended_at = datetime.now(timezone.utc)
                    run.total_api_calls = sum(a.api_call_count for a in agents)
                    run.total_messages = sum(a.message_count for a in agents)
                    await db.commit()

        # Update working memories
        if session_factory and simulation_run_id:
            logger.info("Updating agent working memories...")
            await sim_engine.update_all_working_memories()

        logger.info("Simulation stopped.")
        logger.info(
            "Summary: %s",
            {a.agent_id: {"messages": a.message_count, "api_calls": a.api_call_count}
             for a in agents},
        )


if __name__ == "__main__":
    app()
