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
import uuid
from datetime import UTC, datetime, timezone

import typer

from src.agent.agent import Agent
from src.agent.ids import WRITER_ENGINE_AUX, set_default_writer_id
from src.agent.simulation import SimulationEngine
from src.agent.slack_client import signal_shutdown
from src.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = typer.Typer()

# M-1 (opus review, audit 2026-09-10): the first SIGTERM/SIGINT delays the
# Slack-abort signal by this many seconds instead of firing it at t=0. A
# typical Slack Retry-After backoff (~10s) would otherwise be aborted
# mid-sleep, leaving `_post_message` to record that post DB-only
# (slack_ts=None) and permanently breaking that thread's Slack mirror -- even
# though the runbook's `docker stop -t 30` grace would have let the sleep
# finish naturally. 20s comfortably fits inside that 30s grace while covering
# the common case; a SECOND signal aborts immediately (see
# `_make_shutdown_handler`).
SHUTDOWN_SLACK_ABORT_GRACE_SECONDS = 20


def _make_shutdown_handler(loop: asyncio.AbstractEventLoop, sim_engine) -> callable:
    """Build the SIGTERM/SIGINT handler for ``_run_simulation``.

    Factored out of ``_run_simulation`` so it can be unit-tested without a real
    DB session factory / agent roster / signal loop (see
    ``tests/unit/test_agent_main_shutdown_grace.py``).

    O-1 (audit 2026-09-10): calls ``slack_client.signal_shutdown()`` directly
    rather than ``shutdown_slack_executor()``. There is now exactly one
    process-wide shutdown event (``slack_client.SHUTDOWN_REQUESTED``), shared
    by every caller regardless of which thread or pool it runs on --
    ``signal_shutdown()`` sets it for everyone, including calls running
    through ``src.services.slack_executor``'s pool. The agent-run process
    does not own that pool's lifecycle (it never calls
    ``shutdown_slack_executor()`` -- its worker threads simply exit with the
    process once their sleeps become interruptible), so signalling the
    shared event is all that is needed here.

    The first call only schedules the abort after
    ``SHUTDOWN_SLACK_ABORT_GRACE_SECONDS`` — request_stop() alone already stops
    the NEXT turn from starting, so there is no need to also abort an
    in-flight retry sleep immediately. A second call (repeated signal) aborts
    right away, in case the operator or a slow shutdown needs it sooner.

    P-2 (opus review, audit 2026-09-10): the scheduled ``call_later`` handle
    is stashed on ``shutdown.state["timer_handle"]`` so ``_finalize_shutdown``
    can cancel it once it is moot (the DB flush finished, so signal_shutdown()
    is about to be called unconditionally anyway) rather than leaving it
    pending against a loop that is about to close.

    S-1 (audit 2026-09-10): this handler is now installed with
    ``signal.signal`` (see ``_run_simulation``), not
    ``loop.add_signal_handler``. ``loop.add_signal_handler`` delivers a
    signal by writing to a self-pipe that the loop only reads from its own
    ``select``/``poll`` wait -- if the loop thread is blocked inside a
    *synchronous* call that never returns control to that wait (e.g.
    ``AgentSlackClient.connect()``'s blocking ``auth.test``, which can sit in
    Slack's retry/backoff loop for up to
    ``RATE_LIMIT_WAIT_BUDGET_SECONDS`` == 180s), the self-pipe is never
    drained and the signal is never delivered at all: no ``request_stop()``,
    no grace timer, no ``signal_shutdown()``, until that one blocking call
    happens to return on its own. ``signal.signal`` handlers are instead
    invoked by CPython's own EINTR-retry machinery (PEP 475) the moment a
    blocking call is interrupted, so they still fire while the loop itself is
    stuck -- the actual bound is one in-flight Slack HTTP request plus <=1s
    of that call's own interruptible retry-sleep slicing, not the previous
    "up to 180s, or never, if the loop is what's blocked" bound.

    Because a true ``signal.signal`` handler can interrupt code that is
    itself mid-mutation of the loop's internals, ``loop.call_later`` (which
    touches the loop's timer heap) is never called directly from inside this
    handler. It is scheduled via ``loop.call_soon_threadsafe`` instead --
    documented by asyncio as safe to call from a signal handler -- so the
    heap is only ever touched on the loop's own turn. ``request_stop()`` has
    no such hazard (it only flips a plain flag, and is documented safe to
    call from a signal handler already), so it still runs synchronously and
    immediately, taking effect for the blocked loop's own book-keeping the
    moment it next checks ``self._running`` -- it does not need the loop to
    be free first. The second signal likewise calls ``signal_shutdown()``
    directly rather than through the loop, since it exists specifically to
    still work when the loop is the thing that is stuck.
    """
    state = {"signals_received": 0, "timer_handle": None}

    def _schedule_grace_timer() -> None:
        # Runs as a loop callback (queued below via call_soon_threadsafe),
        # never directly inside the signal handler -- see the docstring
        # above for why loop.call_later must only ever run on the loop's own
        # turn.
        state["timer_handle"] = loop.call_later(
            SHUTDOWN_SLACK_ABORT_GRACE_SECONDS, signal_shutdown
        )

    def shutdown(signum=None, frame=None) -> None:
        logger.info("Received shutdown signal")
        # Safe (and immediate) even if the loop is currently blocked inside a
        # synchronous Slack call: this only flips a flag, no loop access.
        sim_engine.request_stop()
        state["signals_received"] += 1
        if state["signals_received"] == 1:
            loop.call_soon_threadsafe(_schedule_grace_timer)
        else:
            # SECOND signal: abort immediately, WITHOUT depending on the loop
            # ever becoming free -- that is exactly what a blocked loop can
            # no longer guarantee (S-1, audit 2026-09-10). signal_shutdown()
            # only sets a threading.Event, safe to call directly here.
            signal_shutdown()

    shutdown.state = state
    return shutdown


def _finalize_shutdown(shutdown: callable) -> None:
    """Unconditionally signal Slack shutdown once teardown has flushed (P-2,
    opus review, audit 2026-09-10).

    Call this from ``_run_simulation``'s ``finally`` block, AFTER
    ``sim_engine.stop()``'s DB flush has run.

    Root cause: the first SIGTERM/SIGINT only *scheduled* the abort via
    ``loop.call_later(SHUTDOWN_SLACK_ABORT_GRACE_SECONDS, signal_shutdown)``.
    If ``sim_engine.start()`` returned before that timer fired — a
    ``--max-runtime`` run finishing on schedule, a clean stop, or a flush
    simply faster than the 20s grace — the timer was dropped when the event
    loop closed and ``SHUTDOWN_REQUESTED`` was never set at all, even though
    the process has already committed to exiting and the DB flush this
    function runs after is complete. Cancels the pending timer handle (now
    moot — this call supersedes it) before setting the event, so it does not
    fire spuriously against a loop that may already be closing.
    """
    try:
        timer_handle = shutdown.state.get("timer_handle")
        if timer_handle is not None:
            timer_handle.cancel()
    except Exception:  # R-4: cancelling is best-effort; the signal is not
        logger.exception("Could not cancel the shutdown grace timer")
    signal_shutdown()


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


async def _reconcile_stale_runs(session_factory, current_run_id: uuid.UUID) -> None:
    """Mark every 'running' SimulationRun other than the current one as 'stopped'.

    A crash, OOM-kill, or `docker kill` can leave a run's status stuck at
    "running" forever: --fresh only inserts a new row and resume's
    `order_by(started_at.desc()).limit(1)` only ever repairs the single
    latest row (issue #25 D2). Called once at startup, after
    simulation_run_id is resolved, from both the --fresh and resume paths.
    """
    from sqlalchemy import update

    from src.models import SimulationRun

    async with session_factory() as db:
        await db.execute(
            update(SimulationRun)
            .where(SimulationRun.status == "running", SimulationRun.id != current_run_id)
            .values(status="stopped", ended_at=datetime.now(UTC))
        )
        await db.commit()


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
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
        from src.database import make_engine
        from src.models import AgentChannel, AgentMessage, PiDmMessage, SimulationRun
        engine = make_engine(settings.database_url)
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

        await _reconcile_stale_runs(session_factory, simulation_run_id)

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
    #
    # The flush must not run in a fire-and-forget task: the main loop can
    # return first, and asyncio.run then cancels the still-pending task
    # mid-await, losing the in-flight turn's messages. It is awaited in the
    # finally-block below instead (R2).
    #
    # O-1 (audit 2026-09-10): there is one process-wide shutdown event
    # (slack_client.SHUTDOWN_REQUESTED), shared by every AgentSlackClient
    # call regardless of which thread or pool it runs on -- whether directly
    # on this event-loop thread (SimulationEngine's roster sync/reconnect
    # paths) or through src.services.slack_executor's pool (_post_message,
    # the channel/DM/proposal-thread pollers, after M-8). _make_shutdown_handler
    # calls slack_client.signal_shutdown() directly; request_stop() alone
    # only stops the NEXT turn from starting, it does not abort a call
    # already sleeping through a Slack Retry-After backoff. The abort is
    # delayed by SHUTDOWN_SLACK_ABORT_GRACE_SECONDS on the first signal (see
    # _make_shutdown_handler) instead of firing at t=0, so a typical ~10s
    # backoff can finish naturally within docker stop -t 30's grace instead
    # of being aborted and leaving that post DB-only. This bound is on the
    # RETRY-SLEEP path only -- see _make_shutdown_handler's docstring for the
    # separate, tighter bound (one in-flight HTTP call plus <=1s) on the
    # signal actually being *delivered* while the loop is blocked.
    #
    # S-1 (audit 2026-09-10): installed with signal.signal, NOT
    # loop.add_signal_handler. add_signal_handler's self-pipe is only ever
    # drained by the loop's own select/poll wait, so it silently never fires
    # while the loop thread is stuck inside a synchronous Slack call (e.g.
    # SimulationEngine._sync_roster_from_db's roster-sync connect() calls,
    # measured able to block up to RATE_LIMIT_WAIT_BUDGET_SECONDS == 180s
    # under a sustained throttle before this fix routed them through
    # run_slack_call) -- no request_stop(), no grace timer, no
    # signal_shutdown() until that call happens to return on its own.
    # signal.signal handlers are instead invoked via CPython's EINTR-retry
    # machinery (PEP 475), so they still fire on the main thread even while
    # it is blocked in that call.
    loop = asyncio.get_event_loop()
    shutdown = _make_shutdown_handler(loop, sim_engine)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, shutdown)

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

        # P-2 (opus review, audit 2026-09-10): set SHUTDOWN_REQUESTED
        # unconditionally now that the flush above is done, regardless of
        # whether a signal was ever received or its grace timer had fired —
        # see _finalize_shutdown's docstring for why relying on that timer
        # alone drops the event on a run that exits before it fires.
        try:
            _finalize_shutdown(shutdown)
        except Exception:  # Q-4: never skip the run-status update below
            logger.exception("Shutdown finalisation failed")

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
