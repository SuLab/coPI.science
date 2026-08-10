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
from datetime import datetime, timezone

import typer

from src.agent.agent import Agent
from src.agent.ids import WRITER_ENGINE_AUX, set_default_writer_id
from src.agent.simulation import SimulationEngine
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
    all_agents: bool = typer.Option(False, "--all-agents", help="Run every AgentRegistry row regardless of status (default is status='active' only)"),
):
    """Run the turn-based agent simulation."""
    # Claim this process's canonical-id writer slot before anything mints. The
    # engine's own minter owns WRITER_ENGINE; the module default is used here
    # only for PI DM rows, so it takes the aux slot (R1).
    set_default_writer_id(WRITER_ENGINE_AUX)
    asyncio.run(_run_simulation(max_runtime, budget, mock, no_db, fresh, reset_cursors, all_agents))


async def _run_simulation(
    max_runtime: int,
    budget: int,
    mock: bool,
    no_db: bool,
    fresh: bool,
    reset_cursors: bool = False,
    all_agents: bool = False,
) -> None:
    settings = get_settings()

    # The roster is sourced entirely from the AgentRegistry table (the DB is the
    # single source of truth). By default we run status=='active' agents; pass
    # --all-agents to include every row regardless of status (the token gate
    # below still drops anyone without a valid bot token). The roster read runs
    # even under --no-db (it is independent of event logging).
    from sqlalchemy import select as _select
    from sqlalchemy.ext.asyncio import async_sessionmaker as _asm, create_async_engine as _cae
    from src.models import AgentRegistry as _AR

    roster_tokens: dict[str, str | None] = {}
    _engine = _cae(settings.database_url)
    try:
        _sf = _asm(_engine, expire_on_commit=False)
        async with _sf() as _db:
            _stmt = _select(
                _AR.agent_id, _AR.bot_name, _AR.pi_name, _AR.slack_bot_token, _AR.role
            )
            if not all_agents:
                _stmt = _stmt.where(_AR.status == "active")
            _rows = (await _db.execute(_stmt.order_by(_AR.agent_id))).all()
    finally:
        await _engine.dispose()

    agents = [
        Agent(agent_id=r.agent_id, bot_name=r.bot_name, pi_name=r.pi_name, role=r.role)
        for r in _rows
    ]
    roster_tokens = {r.agent_id: r.slack_bot_token for r in _rows}

    if not agents:
        logger.error(
            "No agents in roster (filter=%s) — nothing to run; exiting.",
            "all statuses (--all-agents)" if all_agents else "status='active'",
        )
        return

    logger.info(
        "Roster: %d agents (%s)",
        len(agents), "all statuses" if all_agents else "status='active'",
    )

    # Resolve whether Slack is enabled. --mock forces it off; an explicit
    # SLACK_ENABLED env setting wins next; otherwise auto-detect from whether
    # any agent has a usable bot token. When off, the DB is the sole store and
    # no Slack API calls are made. See specs/local-db-conversations.md.
    from src.services.slack_tokens import env_token, is_valid_token

    def _token_for(agent_id: str) -> str | None:
        tok = roster_tokens.get(agent_id)
        return tok if is_valid_token(tok) else env_token(agent_id)

    if mock:
        slack_enabled = False
    elif settings.slack_enabled is not None:
        slack_enabled = settings.slack_enabled
    else:
        slack_enabled = any(is_valid_token(_token_for(a.agent_id)) for a in agents)

    # Set up transports. When Slack is on, each agent gets a Web-API client
    # (Web API only, no Socket Mode); when off, a NullTransport that no-ops all
    # Slack calls so the engine runs identically against the DB.
    slack_clients = {}
    if slack_enabled:
        from src.agent.slack_client import AgentSlackClient
        for agent in agents:
            bot_token = _token_for(agent.agent_id)
            if is_valid_token(bot_token):
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
    else:
        from src.agent.transport import NullTransport
        for agent in agents:
            slack_clients[agent.agent_id] = NullTransport(agent_id=agent.agent_id)
        logger.info("Slack disabled — running DB-only (NullTransport for %d agents)", len(agents))

    # Set up database session factory
    session_factory = None
    simulation_run_id = None

    if not no_db:
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from src.models import AgentChannel, AgentMessage, PiDmMessage, SimulationRun
        engine = create_async_engine(settings.database_url)
        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        if fresh:
            # Wipe simulation data for a clean start
            # Preserve thread_decisions and proposal_reviews (PI-facing review data)
            logger.info("--fresh: wiping simulation data (preserving proposals and reviews)...")
            async with session_factory() as db:
                await db.execute(AgentMessage.__table__.delete())
                await db.execute(AgentChannel.__table__.delete())
                await db.execute(PiDmMessage.__table__.delete())
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
        slack_enabled=slack_enabled,
    )

    # Handle shutdown signals
    loop = asyncio.get_event_loop()

    def shutdown():
        # Only flip the stop flag here. The flush must not run in a
        # fire-and-forget task: the main loop can return first, and asyncio.run
        # then cancels the still-pending task mid-await, losing the in-flight
        # turn's messages. It is awaited in the finally-block below instead (R2).
        logger.info("Received shutdown signal")
        sim_engine.request_stop()

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
        # Durably flush buffered messages/LLM logs before anything else. The DB
        # is the primary conversation store, so anything still in the in-memory
        # buffer at exit is otherwise unrecoverable. Runs on every exit path
        # (signal, time limit, budget exhaustion, crash).
        try:
            await sim_engine.stop()
        except Exception:
            logger.exception("Final flush on shutdown failed")

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

        logger.info("Simulation stopped.")
        logger.info(
            "Summary: %s",
            {a.agent_id: {"messages": a.message_count, "api_calls": a.api_call_count}
             for a in agents},
        )


if __name__ == "__main__":
    app()
