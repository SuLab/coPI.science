"""Always-on control-plane consumer for the agent container.

Replaces the operator's `docker compose run ... python -m src.agent.main` with
an idle loop: the container stays up, and a run starts ONLY when an explicit
`simulation_commands` row (written by /admin/simulation) is claimed. Boot
marks every pending command stale — the never-auto-start invariant: a reboot,
redeploy, or crash-restart must never replay a pre-boot request.

The running engine handles `stop` itself (`_poll_control_plane`). Liveness is
the engine advisory lock (`engine_alive`, spec §8.3): while an engine holds it,
this loop leaves stops for it, refuses a start ("engine already running") and
never writes `idle` or `starting` over its heartbeat. Boot stales every pending
command except a Finalize run `stop`, which — like every stop still pending
after a run — is settled by `_settle_pending_stops`: a finalize stop runs
`HeadlineAnnouncer.finalize` under the engine lock, any other finishes "run
already stopped". The loop EXITS after each completed run
(audit V5):
`restart: unless-stopped` brings the process back up idle through the
boot-staling path, which is what keeps `docker stop -t 420`'s documented
semantics — mid-run, _run_simulation's own SIGTERM handler stops the engine
gracefully, the run returns, the process exits, and `docker stop` returns
well inside the grace period instead of idling into a SIGKILL.
"""
import asyncio
import logging
import signal
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agent.engine.headlines import build_finalize_announcer
from src.agent.ids import WRITER_ENGINE_AUX, set_default_writer_id
from src.agent.main import _run_simulation
from src.config import get_settings
from src.services.advisory_locks import ENGINE_LOCK_KEY, SessionAdvisoryLock
from src.services.simulation_control import (
    claim_pending,
    engine_alive,
    finish_command,
    is_finalize_stop,
    mark_pending_stale,
    upsert_status,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

POLL_SECONDS = 5.0
_shutdown = False


def _request_shutdown() -> None:
    global _shutdown
    logger.info("Supervisor received shutdown signal")
    _shutdown = True


def _install_signal_handlers(loop) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _request_shutdown)


_LOCK_HELD = "engine lock held — use Finalize run when no engine is alive"


async def _run_finalize(db, cmd, database_url: str, session_factory) -> None:
    """Finalize run for a claimed ``stop`` carrying ``finalize`` and ``run_id``
    (spec §8.2). Holds ``pg_try_advisory_lock(<engine key>)`` for the routine's
    whole duration, so no engine can start or resume meanwhile; if the lock is
    held (a CLI engine, or ``run_fn`` raised ``EngineAlreadyRunning``), the
    command fails instead of waiting for a live engine that would only reject
    it. ``db`` is the session that claimed ``cmd``: its row lock is held until
    ``finish_command`` commits."""
    lock = SessionAdvisoryLock(database_url, ENGINE_LOCK_KEY)
    try:
        if not await lock.acquire():
            await finish_command(db, cmd.id, status="failed", result=_LOCK_HELD)
            return
        try:
            run_id = uuid.UUID(str((cmd.payload or {})["run_id"]))
            announcer = await build_finalize_announcer(session_factory, run_id)
            report = await announcer.finalize()
            outcome = "done"
            result = (
                f"finalized run {run_id}: {len(report.posted)} posted, "
                f"{len(report.lost)} lost, {len(report.in_doubt)} in doubt"
            )
        except Exception as exc:  # noqa: BLE001 — the supervisor must survive
            logger.exception("Finalize run failed")
            outcome, result = "failed", f"{type(exc).__name__}: {exc}"[:500]
        await finish_command(db, cmd.id, status=outcome, result=result)
    finally:
        await lock.release()


async def _settle_pending_stops(
    session_factory, database_url: str, *, finalize_only: bool = False,
) -> int:
    """Claim pending ``stop`` commands oldest first (SA6-05): a finalize stop
    runs the finalize routine; any other is finished ``done`` ("run already
    stopped"). After ``run_fn`` returns this settles EVERY stop, so a stop
    enqueued after the loop's last control poll is never left behind for the
    next boot to discard (SA5-02). A non-finalize stop is finished only when no
    engine is alive: a CLI engine may hold the lock and owns that stop, so it
    is left pending for it. At boot (``finalize_only=True``) it runs only the
    finalize stops the boot stale spared, and leaves any other stop — one
    enqueued after the stale — to the main loop, which knows whether an engine
    is alive. Returns the number settled."""
    settled = 0
    while True:
        async with session_factory() as db:
            cmd = await claim_pending(db, command="stop")
            if cmd is None:
                return settled
            if is_finalize_stop(cmd):
                await _run_finalize(db, cmd, database_url, session_factory)
            elif finalize_only or await engine_alive(db):
                await db.rollback()  # release the claim; the main loop / live engine decides
                return settled
            else:
                await finish_command(db, cmd.id, status="done", result="run already stopped")
            settled += 1


async def run_supervisor(session_factory=None, run_fn=None, poll_seconds=POLL_SECONDS,
                         max_loops=None, database_url: str | None = None) -> None:
    global _shutdown
    _shutdown = False
    run_fn = run_fn or _run_simulation
    database_url = database_url or get_settings().database_url
    own_engine = None
    if session_factory is None:
        own_engine = create_async_engine(database_url, pool_pre_ping=True)
        session_factory = async_sessionmaker(own_engine, expire_on_commit=False)
    loop = asyncio.get_running_loop()
    try:
        _install_signal_handlers(loop)
    except (NotImplementedError, RuntimeError):
        pass  # test event loops without signal support
    try:
        async with session_factory() as db:
            staled = await mark_pending_stale(
                db, reason="supervisor boot — request again", spare_finalize=True,
            )
            if not await engine_alive(db):
                await upsert_status(db, state="idle")
                await db.commit()
        if staled:
            logger.warning("Boot: marked %d pre-boot pending command(s) stale", staled)
        await _settle_pending_stops(session_factory, database_url, finalize_only=True)
        loops = 0
        while not _shutdown and (max_loops is None or loops < max_loops):
            loops += 1
            async with session_factory() as db:
                # The engine lock is the liveness signal (spec §8.3): a live
                # engine — this process's own run or a CLI emergency run — owns
                # its stops and its heartbeat row.
                alive = await engine_alive(db)
                stop_cmd = await claim_pending(db, command="stop")
                if stop_cmd is not None:
                    if alive:
                        logger.info("Leaving stop %s for the live engine", stop_cmd.id)
                    elif is_finalize_stop(stop_cmd):
                        await _run_finalize(db, stop_cmd, database_url, session_factory)
                    else:
                        await finish_command(db, stop_cmd.id, status="done",
                                             result="nothing running")
                cmd = await claim_pending(db, command="start")
                if cmd is None:
                    if not alive:
                        await upsert_status(db, state="idle")
                        await db.commit()
                elif alive:
                    await finish_command(db, cmd.id, status="failed",
                                         result="engine already running")
                    cmd = None
                else:
                    payload = cmd.payload or {}
                    fresh = bool(payload.get("fresh", False))
                    max_runtime = int(payload.get("max_runtime", 0))
                    max_proposals = int(payload.get("max_proposals", 0))
                    await upsert_status(db, state="starting",
                                        detail={"fresh": fresh, "max_runtime": max_runtime,
                                                "max_proposals": max_proposals})
                    await db.commit()
                    cmd_id = cmd.id
            if cmd is not None and _shutdown:
                # Claimed a `start` (and even upserted "starting") in the
                # window above, but a shutdown signal landed before we got
                # here to actually run it — never launch a run into a
                # process that is already on its way out. Finish it stale
                # rather than either running it anyway or leaving it
                # claimed-but-pending forever; boot staling on the next
                # start would eventually clear it, but that would silently
                # discard the operator's request rather than telling them
                # to ask again now.
                async with session_factory() as db:
                    await finish_command(
                        db, cmd_id, status="stale",
                        result="supervisor shutting down — request again",
                    )
                logger.info("Shutdown mid-claim — start %s finished stale", cmd_id)
            elif cmd is not None:
                try:
                    await run_fn(max_runtime, 0, False, False, fresh, False, False, max_proposals)
                    outcome, result = "done", "run completed"
                except Exception as exc:  # noqa: BLE001 — the loop must survive a run
                    logger.exception("Simulation run raised")
                    outcome, result = "failed", f"{type(exc).__name__}: {exc}"[:500]
                # The engine replaced our signal handlers with its own; put ours
                # back so a SIGTERM during the settle below (a finalize can post
                # to Slack) is not swallowed by the engine's spent handler.
                try:
                    _install_signal_handlers(loop)
                except (NotImplementedError, RuntimeError):
                    pass
                async with session_factory() as db:
                    await finish_command(db, cmd_id, status=outcome, result=result)
                    # A start that slipped in while the run was live raced the
                    # page's is-running refusal — stale it, never run it
                    # (audit V7).
                    await mark_pending_stale(
                        db, reason="requested while a run was live — request again",
                        command="start",
                    )
                    if not await engine_alive(db):
                        await upsert_status(db, state="idle")
                        await db.commit()
                await _settle_pending_stops(session_factory, database_url)
                # EXIT after each completed run (audit V5): the exit restores
                # `docker stop`'s documented semantics; `restart:
                # unless-stopped` brings us back idle via boot staling —
                # never-auto-start holds.
                logger.info("Run finished (%s) — exiting for a clean restart", outcome)
                break
            await asyncio.sleep(poll_seconds)
    finally:
        if own_engine is not None:
            await own_engine.dispose()


def main() -> None:
    set_default_writer_id(WRITER_ENGINE_AUX)
    asyncio.run(run_supervisor())


if __name__ == "__main__":
    main()
