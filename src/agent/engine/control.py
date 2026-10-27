"""The operator control plane poll and the engine liveness heartbeat task (spec §7.1, §8.3):
claim a pending stop or a per-system command, persist the run's sessions, refresh the
heartbeat row."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC
from typing import Any

from src.agent.engine import deps
from src.agent.engine.constants import CONTROL_POLL_INTERVAL, REQUIRED_MAX_THREAD_MESSAGES
from src.agent.engine.context import EngineContext, RunState, via
from src.agent.sessions import STOP_NOW, SYSTEM_LABELS, SYSTEMS, WIND_DOWN, Limits
from src.services.simulation_control import upsert_status

logger = logging.getLogger("src.agent.simulation")


class EngineAlreadyRunning(RuntimeError):
    """Another process holds the engine advisory lock (B4). Raised before any
    write and before working memory is archived (SA3-08)."""


class EngineConfigError(RuntimeError):
    """A setting the engine cannot run under (spec §8.4 AG-7)."""


HEARTBEAT_INTERVAL_S = 30.0
DRIFT_CHECK_INTERVAL_S = 60.0


def validate_engine_settings(settings) -> None:
    """Engine-start checks that must not live in ``Settings`` (SA3-34)."""
    if settings.max_thread_messages != REQUIRED_MAX_THREAD_MESSAGES:
        raise EngineConfigError(
            f"max_thread_messages={settings.max_thread_messages}; the engine runs only "
            f"with {REQUIRED_MAX_THREAD_MESSAGES} (the phase guidance's CONCLUDE turn "
            "is written for it). Remove MAX_THREAD_MESSAGES from .env."
        )


VERDICT_SCHEMA_CONSTRAINT = "uq_opportunity_assessments_run_thread"


async def require_verdict_schema(db) -> None:
    """Refuse an engine start on a schema without migration 0055's
    ``uq_opportunity_assessments_run_thread``: the verdict upsert's
    ``ON CONFLICT (simulation_run_id, thread_id)`` needs it, so on 0054 every
    threaded verdict write would fail. Raises ``EngineConfigError``."""
    from sqlalchemy import text

    present = await db.scalar(text(
        "SELECT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = :name "
        "AND conrelid = to_regclass('opportunity_assessments'))"
    ), {"name": VERDICT_SCHEMA_CONSTRAINT})
    if not present:
        raise EngineConfigError(
            f"the database has no {VERDICT_SCHEMA_CONSTRAINT} constraint (migration 0055), "
            "so every threaded verdict write would fail. Apply the migrations "
            "(scripts/migrate/run_migration.sh) before starting the engine."
        )


class EngineHeartbeat:
    """The engine's liveness task (spec §8.3).

    Started immediately after the engine lock is taken, in state ``starting``;
    ``mark_running`` switches it when the main loop begins; ``stopping`` once
    ``request_stop`` was called. Every tick it (1) runs ``SELECT 1`` on the lock
    connection — a failure means the lock is gone, so it requests the HOLD end
    reason ``lock_lost`` — and (2) upserts the status row with the run id and
    detail (the control poll's last snapshot, plus drift flags, rubric hashes
    and the circuit breaker). It claims NO commands and never calls
    ``request_stop`` for one: stops are claimed only by ``_poll_control_plane``
    at today's point and cadence (B24, FA-4 N2). Cancelled and awaited after
    ``stop()``'s final flush."""

    def __init__(
        self, *, lock, run_state, session_factory,
        interval: float = HEARTBEAT_INTERVAL_S,
        drift_interval: float = DRIFT_CHECK_INTERVAL_S,
    ) -> None:
        self._lock = lock
        self._run_state = run_state
        self._sf = session_factory
        self._interval = interval
        self._drift_interval = drift_interval
        self._phase = "starting"
        self._run_id = None
        self.tick_detail: dict[str, Any] = {}
        self._circuit_source: Callable[[], bool] | None = None
        self._drift_source: Callable[[], dict] | None = None
        self._drift: dict[str, Any] = {}
        self._rubric: dict[str, Any] | None = None
        self._last_drift_at = float("-inf")
        self._task: asyncio.Task | None = None

    def set_run_id(self, run_id) -> None:
        self._run_id = run_id

    def mark_running(self) -> None:
        self._phase = "running"

    def set_circuit_source(self, fn: Callable[[], bool]) -> None:
        self._circuit_source = fn

    def set_drift_source(self, fn: Callable[[], dict]) -> None:
        self._drift_source = fn

    def state(self) -> str:
        return "stopping" if self._run_state.stop_event.is_set() else self._phase

    def extra_detail(self) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if self._circuit_source is not None:
            extra["circuit_open"] = bool(self._circuit_source())
        if self._drift:
            extra["prompt_drift"] = dict(self._drift)
        if self._rubric is not None:
            extra["rubric"] = dict(self._rubric)
        return extra

    def detail(self) -> dict[str, Any]:
        return {**self.tick_detail, **self.extra_detail()}

    async def tick(self, now: float | None = None) -> None:
        try:
            await self._lock.check()
        except Exception as exc:  # noqa: BLE001 — any failure means the lock is gone
            logger.error(
                "[control] the engine lock connection failed (%s) — the lock is lost; "
                "stopping and holding open interviews (lock_lost)", exc,
            )
            self._run_state.request_stop("lock_lost")
        now = deps.time.monotonic() if now is None else now
        if self._drift_source is not None and now - self._last_drift_at >= self._drift_interval:
            self._last_drift_at = now
            try:
                found = self._drift_source()
                self._drift = found.get("prompt_drift") or {}
                self._rubric = found.get("rubric")
            except Exception as exc:  # noqa: BLE001 — drift is advisory
                logger.warning("[control] prompt drift check failed: %s", exc)
        if self._sf is None:
            return
        try:
            async with self._sf() as db:
                await upsert_status(
                    db, state=self.state(), simulation_run_id=self._run_id, detail=self.detail(),
                )
        except Exception as exc:  # noqa: BLE001 — a missed heartbeat, never the run
            logger.warning("[control] heartbeat upsert failed: %s", exc)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="engine-heartbeat")

    async def _loop(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(self._interval)

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class Control:
    """The operator control-plane poll."""

    agents = via("ctx")
    session_factory = via("ctx")
    simulation_run_id = via("ctx")
    request_stop = via("_run_state")

    OWNED_STATE: tuple[str, ...] = ("_last_control_poll",)

    def __init__(
        self, ctx: EngineContext, *, run_state: RunState,
        heartbeat: EngineHeartbeat | None = None,
    ) -> None:
        self.ctx = ctx
        self._run_state = run_state
        self._heartbeat = heartbeat
        # Last wall-clock time the control plane was polled (claim a pending
        # stop command, refresh the heartbeat row). See _poll_control_plane.
        self._last_control_poll: float = 0.0

    async def _poll_control_plane(self, now: float) -> None:
        """Claim a pending operator command, persist the sessions and refresh the heartbeat.

        Gated on its own `_last_control_poll`/`CONTROL_POLL_INTERVAL`, the same
        way `_sync_roster_from_db` gates on `_last_roster_poll`/
        `ROSTER_POLL_INTERVAL` — the caller (`_run_main_loop`) invokes this
        every tick and the cadence lives here, not at the call site.

        The whole body sits in one try/except: this is a support surface for
        `/workspace/simulation`, not the simulation itself, so a DB hiccup here
        must cost at most one missed heartbeat, never the run.

        A pending `stop` is claimed, marked done, and turned into a real
        `request_stop()` — then the heartbeat is upserted as "stopping", so the
        state row reflects the shutdown that is now underway rather than lagging
        a tick behind it. A `stop` carrying `finalize` is finished `failed` and
        the run continues. With no stop claimed, at most one pending `system`
        command (an operator's per-system start, wind down or stop now) is
        claimed and finished by `_apply_system_command`; the orchestrator applies
        the session events it causes right after this poll.

        Then the sessions are written to the run's row when they changed
        (`_persist_sessions`), and state "running" (or "stopping") is upserted
        with a `detail` snapshot (`tick_at`, per-agent `active_threads`/
        `calls_in_window`/`api_calls`/`messages`, `roster_size`, and `systems`:
        each system's latest session) so a human watching `/workspace/simulation`
        can see the engine is alive, how loaded it is and which systems run.
        """
        if not self.session_factory:
            return
        if now - self._last_control_poll < CONTROL_POLL_INTERVAL:
            return
        self._last_control_poll = now

        try:
            from src.services.simulation_control import (
                claim_pending,
                finish_command,
                is_finalize_stop,
            )

            async with self.session_factory() as db:
                stopping = False
                cmd = await claim_pending(db, command="stop")
                if cmd is not None and is_finalize_stop(cmd):
                    # Finalize run applies to a STOPPED run (spec §8.2): a live
                    # engine fails it and keeps running, whatever its run_id.
                    await finish_command(
                        db, cmd.id, status="failed",
                        result="Finalize run applies to a stopped run",
                    )
                    cmd = None
                if cmd is not None:
                    hold = bool((cmd.payload or {}).get("hold_open"))
                    await finish_command(
                        db, cmd.id, status="done",
                        result=(
                            f"run {self.simulation_run_id} (hold open interviews)"
                            if hold else f"run {self.simulation_run_id}"
                        ),
                    )
                    self.request_stop("operator_hold" if hold else "operator")
                    stopping = True
                else:
                    system_cmd = await claim_pending(db, command="system")
                    if system_cmd is not None:
                        status, result = self._apply_system_command(system_cmd)
                        await finish_command(db, system_cmd.id, status=status, result=result)

                if self.ctx.sessions.dirty:
                    await self._persist_sessions(db)

                detail = self._tick_detail()
                if self._heartbeat is not None:
                    # One detail for both writers, so the page never flickers
                    # between the tick snapshot and the heartbeat's flags.
                    self._heartbeat.tick_detail = detail
                    detail = {**detail, **self._heartbeat.extra_detail()}
                await upsert_status(
                    db, state="stopping" if stopping else "running",
                    simulation_run_id=self.simulation_run_id, detail=detail,
                )
        except Exception as exc:
            logger.warning("[control] poll failed: %s", exc)

    def _tick_detail(self) -> dict[str, Any]:
        """The control poll's heartbeat snapshot (see `_poll_control_plane`)."""
        return {
            "tick_at": deps.datetime.now(UTC).isoformat(),
            "agents": {
                aid: {
                    "active_threads": len(a.state.active_threads),
                    "calls_in_window": len(a.state.call_times),
                    "api_calls": a.api_call_count,
                    "messages": a.message_count,
                }
                for aid, a in self.agents.items()
            },
            "roster_size": len(self.agents),
            "systems": self.ctx.sessions.detail(),
        }

    def _apply_system_command(self, cmd) -> tuple[str, str]:
        """Apply one claimed `system` command to the sessions; return the command's
        `(status, result)`.

        The payload is ``{"action": "start"|"wind_down"|"stop_now", "system": …,
        "run_id": …, "limits": {…}}``. A command for another run is `stale` (the
        operator asked about a run that has since ended). A start of a running
        system, or a wind down or stop now of one that is not running, is
        `failed` with the reason the panel shows; so is a malformed payload.
        """
        payload = cmd.payload if isinstance(cmd.payload, dict) else {}
        run_id = payload.get("run_id")
        if str(run_id) != str(self.simulation_run_id):
            return "stale", f"for run {run_id}, not this one"
        system = payload.get("system")
        action = payload.get("action")
        if system not in SYSTEMS:
            return "failed", f"unknown system {system!r}"
        label = SYSTEM_LABELS[system]
        now = deps.datetime.now(UTC)
        if action == "start":
            try:
                self.ctx.sessions.start(system, Limits.from_mapping(payload.get("limits")), now)
            except ValueError:
                return "failed", f"{label} is already running"
            return "done", f"{label} started in run {self.simulation_run_id}"
        if action == "wind_down":
            if not self.ctx.sessions.wind_down(system, WIND_DOWN, now):
                return "failed", f"{label} is not running"
            return "done", f"{label} winding down in run {self.simulation_run_id}"
        if action == "stop_now":
            if not self.ctx.sessions.end(system, STOP_NOW, now):
                return "failed", f"{label} is not running"
            return "done", f"{label} stopped in run {self.simulation_run_id}"
        return "failed", f"unknown action {action!r}"

    async def _persist_sessions(self, db) -> None:
        """Write the session records to this run's ``simulation_runs.config["sessions"]``
        and mark the sessions clean. ``config`` is a plain JSON column, so it is
        reassigned, never changed in place (an in-place change is not flushed). The
        sessions are marked clean before the write, so a change made while it is in
        flight is written next time; a failed write marks them dirty again. Never
        raises: one warning."""
        if not self.simulation_run_id:
            return
        from src.models import SimulationRun

        sessions = self.ctx.sessions
        records = sessions.records()
        sessions.mark_clean()
        try:
            run = await db.get(SimulationRun, self.simulation_run_id)
            if run is None:
                raise LookupError(f"no simulation_runs row {self.simulation_run_id}")
            run.config = {**(run.config or {}), "sessions": records}
            await db.commit()
        except Exception as exc:  # noqa: BLE001 — a missed write, never the run
            sessions.dirty = True
            with contextlib.suppress(Exception):
                await db.rollback()
            logger.warning(
                "[control] could not record run %s's sessions: %s", self.simulation_run_id, exc,
            )

    async def persist_sessions(self) -> None:
        """``_persist_sessions`` in a session of its own, when the sessions changed:
        ``stop()`` calls it after the sessions were ended. Never raises."""
        if not self.session_factory or not self.simulation_run_id or not self.ctx.sessions.dirty:
            return
        try:
            async with self.session_factory() as db:
                await self._persist_sessions(db)
        except Exception as exc:  # noqa: BLE001 — opening the session failed
            logger.warning(
                "[control] could not record run %s's sessions: %s", self.simulation_run_id, exc,
            )
