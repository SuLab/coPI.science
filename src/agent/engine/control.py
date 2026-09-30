"""The operator control plane poll: claim a pending stop, refresh the heartbeat row (spec §7.1). Phase 2 adds the engine lock and the heartbeat task here."""

from __future__ import annotations

import logging
from datetime import UTC

from src.agent.engine import deps
from src.agent.engine.constants import CONTROL_POLL_INTERVAL
from src.agent.engine.context import EngineContext, RunState, via

logger = logging.getLogger("src.agent.simulation")


class Control:
    """The operator control-plane poll."""

    agents = via("ctx")
    session_factory = via("ctx")
    simulation_run_id = via("ctx")
    request_stop = via("_run_state")

    OWNED_STATE: tuple[str, ...] = ("_last_control_poll",)

    def __init__(self, ctx: EngineContext, *, run_state: RunState) -> None:
        self.ctx = ctx
        self._run_state = run_state
        # Last wall-clock time the control plane was polled (claim a pending
        # stop command, refresh the heartbeat row). See _poll_control_plane.
        self._last_control_poll: float = 0.0

    async def _poll_control_plane(self, now: float) -> None:
        """Claim a pending operator `stop` command and refresh the heartbeat.

        Gated on its own `_last_control_poll`/`CONTROL_POLL_INTERVAL`, the same
        way `_sync_roster_from_db` gates on `_last_roster_poll`/
        `ROSTER_POLL_INTERVAL` — the caller (`_run_main_loop`) invokes this
        every tick and the cadence lives here, not at the call site.

        The whole body sits in one try/except: this is a support surface for
        `/admin/simulation`, not the simulation itself, so a DB hiccup here
        must cost at most one missed heartbeat, never the run.

        With no pending `stop`, upserts state "running" with a `detail` snapshot
        (`tick_at`, per-agent `active_threads`/`calls_in_window`/`api_calls`/
        `messages`, and `roster_size`) so a human watching `/admin/simulation`
        can see the engine is alive and how loaded it is. A pending `stop` is
        claimed, marked done, and turned into a real `request_stop()` — then the
        heartbeat is upserted as "stopping" instead, so the state row reflects
        the shutdown that is now underway rather than lagging a tick behind it.
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
                upsert_status,
            )

            detail = {
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
            }

            async with self.session_factory() as db:
                cmd = await claim_pending(db, command="stop")
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
                    await upsert_status(
                        db, state="stopping",
                        simulation_run_id=self.simulation_run_id, detail=detail,
                    )
                else:
                    await upsert_status(
                        db, state="running",
                        simulation_run_id=self.simulation_run_id, detail=detail,
                    )
        except Exception as exc:
            logger.warning("[control] poll failed: %s", exc)
