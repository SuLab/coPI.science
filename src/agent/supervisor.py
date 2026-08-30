"""Always-on control-plane consumer for the agent container.

Replaces the operator's `docker compose run ... python -m src.agent.main` with
an idle loop: the container stays up, and a run starts ONLY when an explicit
`simulation_commands` row (written by /admin/simulation) is claimed. Boot
marks every pending command stale — the never-auto-start invariant: a reboot,
redeploy, or crash-restart must never replay a pre-boot request.

The running engine handles `stop` itself (SimulationEngine._poll_control_plane)
— while idle, this loop clears orphaned `stop`s ONLY when no fresh `running`
heartbeat exists (a live heartbeat means a CLI-emergency engine owns that
stop; audit V4). The loop EXITS after each completed run (audit V5):
`restart: unless-stopped` brings the process back up idle through the
boot-staling path, which is what keeps `docker stop -t 420`'s documented
semantics — mid-run, _run_simulation's own SIGTERM handler stops the engine
gracefully, the run returns, the process exits, and `docker stop` returns
well inside the grace period instead of idling into a SIGKILL.
"""
import asyncio
import logging
import signal
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agent.ids import WRITER_ENGINE_AUX, set_default_writer_id
from src.agent.main import _run_simulation
from src.config import get_settings
from src.services.simulation_control import (
    claim_pending,
    derive_panel_state,
    finish_command,
    mark_pending_stale,
    read_status,
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


async def run_supervisor(session_factory=None, run_fn=None, poll_seconds=POLL_SECONDS,
                         max_loops=None) -> None:
    global _shutdown
    _shutdown = False
    run_fn = run_fn or _run_simulation
    own_engine = None
    if session_factory is None:
        own_engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
        session_factory = async_sessionmaker(own_engine, expire_on_commit=False)
    loop = asyncio.get_running_loop()
    try:
        _install_signal_handlers(loop)
    except (NotImplementedError, RuntimeError):
        pass  # test event loops without signal support
    try:
        async with session_factory() as db:
            staled = await mark_pending_stale(db, reason="supervisor boot — request again")
            await upsert_status(db, state="idle")
            await db.commit()
        if staled:
            logger.warning("Boot: marked %d pre-boot pending command(s) stale", staled)
        loops = 0
        while not _shutdown and (max_loops is None or loops < max_loops):
            loops += 1
            async with session_factory() as db:
                # Stops FIRST, so a stale stop can never survive into a run
                # this iteration is about to start; and a stop aimed at a
                # live foreign engine (CLI emergency path) is left pending
                # for that engine's own control poll — a fresh `running`
                # heartbeat is the tell (audit V4).
                stop_cmd = await claim_pending(db, command="stop")
                if stop_cmd is not None:
                    if derive_panel_state(await read_status(db), datetime.now(UTC)) == "running":
                        logger.info("Leaving stop %s for the live engine", stop_cmd.id)
                    else:
                        await finish_command(db, stop_cmd.id, status="done",
                                             result="nothing running")
                cmd = await claim_pending(db, command="start")
                if cmd is None:
                    # Idle heartbeat: without this the page reads a healthy
                    # idle supervisor as "stale" two minutes after boot
                    # (audit V3).
                    await upsert_status(db, state="idle")
                    await db.commit()
                else:
                    payload = cmd.payload or {}
                    fresh = bool(payload.get("fresh", False))
                    max_runtime = int(payload.get("max_runtime", 0))
                    await upsert_status(db, state="starting",
                                        detail={"fresh": fresh, "max_runtime": max_runtime})
                    await db.commit()
                    cmd_id = cmd.id
            if cmd is not None:
                try:
                    await run_fn(max_runtime, 0, False, False, fresh, False, False)
                    outcome, result = "done", "run completed"
                except Exception as exc:  # noqa: BLE001 — the loop must survive a run
                    logger.exception("Simulation run raised")
                    outcome, result = "failed", f"{type(exc).__name__}: {exc}"[:500]
                async with session_factory() as db:
                    await finish_command(db, cmd_id, status=outcome, result=result)
                    # A start that slipped in while the run was live raced the
                    # page's is-running refusal — stale it, never run it
                    # (audit V7).
                    await mark_pending_stale(
                        db, reason="requested while a run was live — request again",
                        command="start",
                    )
                    await upsert_status(db, state="idle")
                    await db.commit()
                # EXIT after each completed run (audit V5): _run_simulation
                # replaced our signal handlers with the engine's, so idling on
                # would leave `docker stop` burning the full grace period into
                # a SIGKILL. Exiting restores the documented semantics;
                # `restart: unless-stopped` brings us back idle via boot
                # staling — never-auto-start holds.
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
