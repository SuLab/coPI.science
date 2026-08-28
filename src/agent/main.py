"""Agent simulation engine entry point.

Usage:
    python -m src.agent.main                        # resume, run until stopped
    python -m src.agent.main --max-runtime 60       # resume, stop after 60 min
    python -m src.agent.main --fresh                 # start a NEW run (deletes nothing)
    python -m src.agent.main --fresh --max-runtime 60
"""

import asyncio
import logging
import signal
import uuid
from datetime import datetime, timezone

import typer

from src.agent.agent import Agent
from src.agent.ids import WRITER_ENGINE_AUX, set_default_writer_id
from src.agent.simulation import SimulationEngine
from src.config import get_settings
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = typer.Typer()


@app.command()
def main(
    max_runtime: int = typer.Option(0, "--max-runtime", help="Max runtime in minutes (0 = run until stopped)"),
    budget: int = typer.Option(
        0, "--budget",
        help=(
            "DEPRECATED legacy cumulative cap: max LLM calls per agent for the "
            "WHOLE run. 0 (default) disables it. Superseded by the sliding-window "
            "rate limiter (llm_calls_per_load_per_window). Passing a nonzero value "
            "can permanently bench a hub agent — see "
            "docs/specs/2026-08-06-hub-budget-scheduler-design.md §6."
        ),
    ),
    mock: bool = typer.Option(False, "--mock", help="Run in mock mode without real Slack tokens"),
    no_db: bool = typer.Option(False, "--no-db", help="Skip database logging"),
    fresh: bool = typer.Option(
        False, "--fresh",
        help=(
            "Start a NEW simulation run instead of resuming the latest. Deletes "
            "nothing: a new simulation_run_id is what isolates the run, and "
            "pre-run Slack history is skipped rather than re-imported. Archives "
            "profiles/memory/* to profiles/memory/archive/<UTC stamp>/ so agents "
            "start with no working memory."
        ),
    ),
    reset_cursors: bool = typer.Option(False, "--reset-cursors", help="Reset scan cursors so agents re-read all posts"),
    all_agents: bool = typer.Option(False, "--all-agents", help="Run every AgentRegistry row regardless of status (default is status='active' only)"),
):
    """Run the turn-based agent simulation."""
    # Claim this process's canonical-id writer slot before anything mints. The
    # engine's own minter owns WRITER_ENGINE; the module default is used here
    # only for PI DM rows, so it takes the aux slot (R1).
    set_default_writer_id(WRITER_ENGINE_AUX)
    asyncio.run(_run_simulation(max_runtime, budget, mock, no_db, fresh, reset_cursors, all_agents))


#: What `SimulationRun.total_api_calls` counts, said where an operator will read
#: it. The column is rendered to humans in three admin templates, and on
#: 2026-08-22 its UNITS changed: `Agent.record_api_call` plus
#: `SimulationEngine._unbooked_calls` now book every real API call (tool rounds
#: and truncation retries included) where the column used to count turns, and the
#: restart rebuild moved with it (`_CALLS_PER_LOG_ROW`). 78.6% of stored
#: `thread_reply` rows are 2+ calls, so the number roughly doubles for reasons
#: that have nothing to do with the run.
#:
#: This sits beside the rubric version/hash line for the same reason that one
#: exists: a change of meaning that is invisible at read time is a change nobody
#: can correct for afterwards.
API_CALL_UNITS_NOTE = (
    "API-call accounting: SimulationRun.total_api_calls counts REAL API CALLS "
    "(tool rounds and truncation retries included), not turns, as of "
    "2026-08-22. It is NOT comparable with any run recorded before that date. "
    "The old per-turn figure is recoverable for any run as "
    "SELECT COUNT(*) FROM llm_call_logs WHERE simulation_run_id = <run>."
)


def _log_api_call_units() -> None:
    """Emit `API_CALL_UNITS_NOTE` into the startup banner.

    A function rather than an inline `logger.info` so the content is assertable
    without standing up a run — see
    tests/unit/test_api_call_accounting.py::test_the_startup_banner_declares_the_api_call_units.
    """
    logger.info("%s", API_CALL_UNITS_NOTE)


def _stamp_run_config(config: dict) -> dict:
    """The run row's own record of which rubric opened it. The startup banner
    already logs these two values; persisting them is what lets the admin run
    dropdown label a run by rubric after the log is gone. Per-assessment
    stamps stay authoritative for individual verdicts."""
    return {
        **config,
        "rubric_version": RUBRIC_VERSION,
        "rubric_content_hash": RUBRIC_CONTENT_HASH,
    }


async def _open_fresh_run(session_factory, config: dict) -> uuid.UUID:
    """Open a new ``SimulationRun`` row for a ``--fresh`` start. DELETES NOTHING.

    ``--fresh`` used to answer "start clean" with three UNFILTERED deletes —
    ``AgentMessage``, ``AgentChannel`` and ``PiDmMessage``, no
    ``simulation_run_id`` predicate on any of them — so every previous run's
    conversation history went with it. Measured 2026-08-22: ``llm_call_logs``
    held 10 runs and ``opportunity_assessments`` 5, while ``agent_messages``
    held **1**. Run 8b64a0e0's 1,354 messages were gone, 57 of 64 assessments
    carried a ``slack_ts`` that resolved to no message, and the assessment
    detail page's interview timeline was empty for 90% of the corpus.

    The new ``simulation_run_id`` minted here is all the isolation a fresh run
    needs — every ``AgentMessage`` read in the startup path and the main loop is
    already run-scoped, ``uq_agent_messages_run_ts`` is
    ``(simulation_run_id, message_ts)`` so re-seeing a Slack ts is a different
    key, and ``agent_channels`` has no unique constraint beyond its PK so
    per-run duplicate ``channel_name`` rows are fine. ``pi_dm_messages`` is a
    dead table: nothing in ``src/`` writes it. ``thread_decisions`` and
    ``proposal_reviews`` reads were the exception until 2026-08-28 and are now
    filtered too.

    ONE read was not run-scoped and had to be fixed alongside this, or "delete
    nothing" would be strictly worse than the bug: see
    ``SimulationEngine._sync_private_channels_from_db``, which without its run
    filter would hand a brand-new run every previous run's private channels.

    Working memory is handled by the caller: ``--fresh`` archives
    ``profiles/memory/*`` to ``profiles/memory/archive/<stamp>/`` (see
    ``src.agent.working_memory_reset``) so a fresh run's agents start with
    none, while plain resumes keep it.
    """
    from src.models import SimulationRun

    async with session_factory() as db:
        run = SimulationRun(status="running", config=_stamp_run_config(config))
        db.add(run)
        await db.commit()
        logger.info("Created new simulation run %s", run.id)
        return run.id


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
    # single source of truth). By default we run the active_roster_select
    # criterion (status=='active', pi_lab linked to a user); pass --all-agents
    # to include every row regardless of status (the token gate
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
            if all_agents:
                _stmt = _select(
                    _AR.agent_id, _AR.bot_name, _AR.pi_name,
                    _AR.slack_bot_token, _AR.role,
                ).order_by(_AR.agent_id)
            else:
                from src.agent.roster_query import active_roster_select
                _stmt = active_roster_select()
            _rows = (await _db.execute(_stmt)).all()
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
            "all statuses (--all-agents)" if all_agents
            else "status='active', pi_lab linked to a user",
        )
        return

    logger.info(
        "Roster: %d agents (%s)",
        len(agents),
        "all statuses (--all-agents)" if all_agents
        else "status='active', pi_lab linked to a user",
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

    if fresh:
        # A fresh run must not carry previous runs' synthesized verdict
        # ledgers into its prompts (they are injected into EVERY system
        # prompt via Agent._compose_working_memory). Archive-and-reset
        # BEFORE the run opens; nothing is deleted. Plain resumes never
        # touch memory. Deliberately outside the no_db branch: --fresh
        # --no-db must start blind too. See docs/audits/
        # 2026-08-28-run-isolation-and-assessment-archive (F1).
        from src.agent.agent import PROFILES_DIR
        from src.agent.working_memory_reset import archive_working_memory

        archived_to = archive_working_memory(PROFILES_DIR / "memory")
        if archived_to is not None:
            logger.info("--fresh: working memory archived to %s", archived_to)
        else:
            logger.info("--fresh: no working memory to archive")

    # Set up database session factory
    session_factory = None
    simulation_run_id = None

    if not no_db:
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from src.models import SimulationRun
        engine = create_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
        )
        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        run_config = {
            "max_runtime": max_runtime,
            "budget_cap": budget,
            "mock": mock,
            "agent_count": len(agents),
            "active_thread_threshold": settings.active_thread_threshold,
            "max_thread_messages": settings.max_thread_messages,
        }

        if fresh:
            simulation_run_id = await _open_fresh_run(session_factory, run_config)
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
                    stamped_hash = (existing_run.config or {}).get("rubric_content_hash")
                    if stamped_hash and stamped_hash != RUBRIC_CONTENT_HASH:
                        logger.warning(
                            "Resuming run %s, which opened under rubric %s (%s); "
                            "this process loaded %s (%s). Per-assessment stamps "
                            "remain authoritative.",
                            existing_run.id,
                            (existing_run.config or {}).get("rubric_version"),
                            stamped_hash,
                            RUBRIC_VERSION,
                            RUBRIC_CONTENT_HASH,
                        )
                    simulation_run_id = existing_run.id
                    existing_run.status = "running"
                    existing_run.ended_at = None
                    await db.commit()
                    logger.info("Resuming simulation run %s", simulation_run_id)
                else:
                    # No existing run — create one
                    run = SimulationRun(status="running", config=_stamp_run_config(run_config))
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
        # A new simulation_run_id isolates this run's DB reads (see
        # _open_fresh_run, which deletes nothing). Slack has no such scoping:
        # without this flag the engine would reconcile every previous run's
        # conversations straight back off the transport and attribute them to
        # this run — see SimulationEngine._restore_slack_state.
        fresh_start=fresh,
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
        if budget > 0:
            logger.warning(
                "--budget %d is the DEPRECATED cumulative cap. It counts LLM calls "
                "for the ENTIRE run, is rebuilt from llm_call_logs on restart, and "
                "therefore benches an agent PERMANENTLY once crossed — this is what "
                "took the blackbird hub off the air for 161 consecutive turns. The "
                "sliding-window rate limiter supersedes it. Pass --budget 0 unless "
                "you specifically want the legacy behaviour.",
                budget,
            )
        logger.info(
            "Starting simulation: %d agents, %s max runtime, %d budget/agent%s",
            len(agents), runtime_label, budget,
            " (fresh start)" if fresh else " (resuming)",
        )
        # Which rubric this run screens against. This line is what makes the
        # rubric document's editing workflow checkable (see the header comment
        # of prompts/rubric/blackbird-rubric.toml, step 3): the document is
        # loaded ONCE at import, so applying an edit means a restart — and the
        # only way to confirm the restart picked the edit up is to compare
        # these two values against the file.
        logger.info(
            "Screening rubric: version %s (content hash %s)",
            RUBRIC_VERSION, RUBRIC_CONTENT_HASH,
        )
        _log_api_call_units()
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
                    # UNITS: real API CALLS, not turns, since 2026-08-22 —
                    # see API_CALL_UNITS_NOTE above. NOT comparable with any
                    # earlier run; the old per-turn figure is COUNT(*) over
                    # llm_call_logs for the same simulation_run_id.
                    run.total_api_calls = sum(a.api_call_count for a in agents)
                    run.total_messages = sum(a.message_count for a in agents)
                    await db.commit()

        logger.info("Simulation stopped.")
        logger.info(
            # "api_calls" here is the same per-agent number that sums into
            # SimulationRun.total_api_calls — real API calls, not turns.
            "Summary (api_calls = real API calls, not turns): %s",
            {a.agent_id: {"messages": a.message_count, "api_calls": a.api_call_count}
             for a in agents},
        )


if __name__ == "__main__":
    app()
