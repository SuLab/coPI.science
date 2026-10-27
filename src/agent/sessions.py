"""Which simulation systems a run is running: one *session* per start of a system.

Two systems share one run (the independent-controls design,
``docs/specs/2026-10-27-independent-simulation-controls-design.md`` §4): *interviews* (labs
pitch; BlackbirdBot interviews and assesses) and *projects* (BlackbirdBot develops a project
with each lab). Each start of a system opens a ``Session`` with its own limits, counted from
that start (SC14). A session is RUNNING, then WINDING_DOWN (nothing new starts; work under way
finishes) and ENDED. The run ends when no session is active, and how the last one ended decides
how the run ends (SC15, ``Sessions.run_end_reason``).

Pure: no database, no Slack, no engine import. The engine keeps one ``Sessions`` on its
``EngineContext``: the control poll starts and stops sessions, the lanes and the projects unit
consult and observe them, the orchestrator applies the side effects of their events, and the
panel reads ``detail()`` from the heartbeat.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

INTERVIEWS = "interviews"
PROJECTS = "projects"
SYSTEMS: tuple[str, ...] = (INTERVIEWS, PROJECTS)
SYSTEM_LABELS = {INTERVIEWS: "Interviews", PROJECTS: "Projects"}

#: ``Session.state``.
RUNNING = "running"
WINDING_DOWN = "winding_down"
ENDED = "ended"

#: ``Session.reason``: why a session is winding down, or why it ended.
LIMIT_COUNT = "limit_count"
LIMIT_TIME = "limit_time"
WIND_DOWN = "wind_down"
STOP_NOW = "stop_now"
FAILURE_STOP = "failure_stop"
STOP_EVERYTHING = "stop_everything"

#: ``SessionEvent.kind``.
STARTED = "started"

#: What the panel and a run's Activity page say about a reason.
REASON_LABELS = {
    LIMIT_COUNT: "reached its count",
    LIMIT_TIME: "reached its runtime",
    WIND_DOWN: "wound down by hand",
    STOP_NOW: "stopped by hand",
    FAILURE_STOP: "stopped after 3 failures in a row",
    STOP_EVERYTHING: "the run stopped",
}

#: Failed projects in a row that wind projects down (SC13; the number is the designer's call).
FAILURES_IN_A_ROW_MAX = 3
#: Consecutive observations with no open work that end a winding-down session. Interviews are
#: observed every main-loop tick, so three quiet ticks (``PROPOSAL_DRAIN_SETTLE_TICKS``, the old
#: drain gate's settle); projects every project tick, from the database, so one.
SETTLE_CHECKS = {INTERVIEWS: 3, PROJECTS: 1}

#: How the run ends when its last session ended on its own (``src/agent/end_reasons.py``).
_RUN_END_REASONS = {
    LIMIT_COUNT: "target_drained",
    LIMIT_TIME: "time_limit",
    FAILURE_STOP: "failure_stop",
}


def _non_negative_int(value: object) -> int:
    """``value`` when it is a non-negative int (never a bool), else 0."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


@dataclass(frozen=True)
class Limits:
    """A session's limits; 0 means none. ``count`` is proposals for interviews and counted
    (Ready or Not agreed) projects for projects."""

    count: int = 0
    runtime_minutes: int = 0

    @classmethod
    def from_mapping(cls, data: object) -> Limits:
        """``{"count": …, "runtime_minutes": …}``; anything missing, negative, a bool or not an
        int counts as 0, and a non-mapping as no limits."""
        if not isinstance(data, Mapping):
            return cls()
        return cls(
            count=_non_negative_int(data.get("count")),
            runtime_minutes=_non_negative_int(data.get("runtime_minutes")),
        )

    def as_dict(self) -> dict[str, int]:
        return {"count": self.count, "runtime_minutes": self.runtime_minutes}


@dataclass
class Session:
    """One start of one system, until it ends."""

    system: str
    limits: Limits
    started_at: datetime
    state: str = RUNNING
    reason: str | None = None
    wound_down_at: datetime | None = None
    ended_at: datetime | None = None
    count: int = 0
    open_work: int = 0
    failures_in_a_row: int = 0
    initial: bool = False
    #: Lab agent ids holding the open work, as last observed (the panel's "Waiting on").
    waiting_on: tuple[str, ...] = ()
    settle_streak: int = field(default=0, repr=False)

    @property
    def active(self) -> bool:
        return self.state in (RUNNING, WINDING_DOWN)

    def record(self) -> dict[str, object]:
        """The session as JSON-able values: the heartbeat's ``detail["systems"]`` entry and one
        item of ``simulation_runs.config["sessions"]``."""
        return {
            "system": self.system,
            "state": self.state,
            "reason": self.reason,
            "started_at": self.started_at.isoformat(),
            "wound_down_at": self.wound_down_at.isoformat() if self.wound_down_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "limits": self.limits.as_dict(),
            "count": self.count,
            "open_work": self.open_work,
            "failures_in_a_row": self.failures_in_a_row,
            "initial": self.initial,
            "waiting_on": list(self.waiting_on),
        }


@dataclass(frozen=True)
class SessionEvent:
    """A transition the orchestrator may have to act on: ``kind`` is ``STARTED``,
    ``WINDING_DOWN`` or ``ENDED``. ``initial`` is True for a session the engine started with;
    ``cleared_hold`` is True for the interviews start that took up a carried hold."""

    system: str
    kind: str
    reason: str | None
    initial: bool
    cleared_hold: bool = False


def _closed_record(record: Mapping) -> dict:
    """A prior record, kept; one whose process died before it ended is marked ended by
    ``STOP_EVERYTHING`` (it cannot still be running in a new engine)."""
    out = dict(record)
    if out.get("state") != ENDED:
        out["state"] = ENDED
        out["reason"] = STOP_EVERYTHING
    return out


class Sessions:
    """Every session of the live run in this engine, plus the records of earlier processes.

    ``hold_carried`` is the resumed run's interview hold that no interviews session has taken up
    yet (spec §4.9): it makes the run end held (``run_end_reason``) and is cleared by the first
    interviews start. ``dirty`` is set by every change a record shows; the control poll persists
    the records and calls ``mark_clean``."""

    def __init__(
        self, *, prior_records: Sequence[Mapping] = (), hold_carried: bool = False,
    ) -> None:
        self.hold_carried = hold_carried
        self.dirty = False
        self._prior = [_closed_record(r) for r in prior_records if isinstance(r, Mapping)]
        self._history: list[Session] = []
        self._ended: list[Session] = []
        self._events: list[SessionEvent] = []

    @classmethod
    def legacy(cls, *, count: int, runtime_minutes: int, now: datetime) -> Sessions:
        """Interviews only, started with the engine: what an engine built without sessions
        (tests, scripts) runs, as before the two systems were separable."""
        sessions = cls()
        sessions.start(
            INTERVIEWS,
            Limits(count=max(0, int(count)), runtime_minutes=max(0, int(runtime_minutes))),
            now,
            initial=True,
        )
        return sessions

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def current(self, system: str) -> Session | None:
        """The system's latest session in this engine, in any state."""
        for session in reversed(self._history):
            if session.system == system:
                return session
        return None

    def active(self, system: str) -> bool:
        session = self.current(system)
        return session is not None and session.active

    def accepting(self, system: str) -> bool:
        """Running and not winding down: new work may start."""
        session = self.current(system)
        return session is not None and session.state == RUNNING

    def winding_down(self, system: str) -> bool:
        session = self.current(system)
        return session is not None and session.state == WINDING_DOWN

    def any_active(self) -> bool:
        return any(session.active for session in self._history)

    def detail(self) -> dict[str, dict]:
        """``{system: record}`` of each system's latest session: the heartbeat's systems."""
        out: dict[str, dict] = {}
        for system in SYSTEMS:
            session = self.current(system)
            if session is not None:
                out[system] = session.record()
        return out

    def records(self) -> list[dict]:
        """The earlier processes' records, then this engine's sessions in start order."""
        return [*self._prior, *(session.record() for session in self._history)]

    def run_end_reason(self) -> str:
        """The ``end_reasons`` reason for a run whose sessions all ended: ``held_over`` while an
        interview hold is carried; else by how the session that ended last ended (its count
        → ``target_drained``, its runtime → ``time_limit``, the failure stop →
        ``failure_stop``, by hand → ``operator``)."""
        if self.hold_carried:
            return "held_over"
        if not self._ended:
            return "operator"
        return _RUN_END_REASONS.get(self._ended[-1].reason or "", "operator")

    def pop_events(self) -> list[SessionEvent]:
        """The transitions since the last call, oldest first."""
        events, self._events = self._events, []
        return events

    def mark_clean(self) -> None:
        self.dirty = False

    # ------------------------------------------------------------------
    # Changing
    # ------------------------------------------------------------------

    def start(
        self, system: str, limits: Limits, now: datetime, *, initial: bool = False,
    ) -> Session:
        """Open a session. ``ValueError`` for an unknown system or one already active. An
        interviews start takes up a carried hold."""
        if system not in SYSTEMS:
            raise ValueError(f"unknown system {system!r}; expected one of {SYSTEMS}")
        if self.active(system):
            raise ValueError(f"{system} is already running")
        session = Session(system=system, limits=limits, started_at=now, initial=initial)
        self._history.append(session)
        cleared = system == INTERVIEWS and self.hold_carried
        if cleared:
            self.hold_carried = False
        self._emit(session, STARTED, cleared_hold=cleared)
        return session

    def add_count(self, system: str, now: datetime, n: int = 1) -> None:
        """Count ``n`` toward an active session's limit; reaching it while running winds the
        session down at once, so nothing new starts between two observations."""
        session = self.current(system)
        if session is None or not session.active or n <= 0:
            return
        session.count += n
        self.dirty = True
        if (session.state == RUNNING and session.limits.count > 0
                and session.count >= session.limits.count):
            self._wind_down(session, LIMIT_COUNT, now)

    def observe(
        self,
        system: str,
        *,
        open_work: int,
        now: datetime,
        count: int | None = None,
        failures_in_a_row: int | None = None,
        waiting_on: Sequence[str] | None = None,
    ) -> None:
        """Record what the system has under way and apply its limits.

        A running session winds down at its count, then its runtime, then (projects only) at
        ``FAILURES_IN_A_ROW_MAX`` failures in a row. A winding-down session, including one that
        just started winding down, ends with its wind-down reason after
        ``SETTLE_CHECKS[system]`` consecutive observations with no open work; open work resets
        the streak. No session, or an ended one: nothing happens."""
        session = self.current(system)
        if session is None or not session.active:
            return
        before = session.record()
        session.open_work = max(0, open_work)
        if count is not None:
            session.count = max(0, count)
        if failures_in_a_row is not None:
            session.failures_in_a_row = max(0, failures_in_a_row)
        if waiting_on is not None:
            session.waiting_on = tuple(sorted(set(waiting_on)))
        if session.state == RUNNING:
            self._apply_limits(session, now)
        if session.state == WINDING_DOWN:
            if session.open_work == 0:
                session.settle_streak += 1
                if session.settle_streak >= SETTLE_CHECKS[session.system]:
                    self._end(session, session.reason or WIND_DOWN, now)
            else:
                session.settle_streak = 0
        if session.record() != before:
            self.dirty = True

    def wind_down(self, system: str, reason: str, now: datetime) -> bool:
        """Running → winding down with ``reason``; False when the system is not running."""
        session = self.current(system)
        if session is None or session.state != RUNNING:
            return False
        self._wind_down(session, reason, now)
        return True

    def end(self, system: str, reason: str, now: datetime) -> bool:
        """Active → ended with ``reason``; False when the system is not active."""
        session = self.current(system)
        if session is None or not session.active:
            return False
        self._end(session, reason, now)
        return True

    def stop_everything(self, now: datetime) -> None:
        """End every active session: the run itself is stopping."""
        for session in self._history:
            if session.active:
                self._end(session, STOP_EVERYTHING, now)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _apply_limits(self, session: Session, now: datetime) -> None:
        limits = session.limits
        if limits.count > 0 and session.count >= limits.count:
            self._wind_down(session, LIMIT_COUNT, now)
        elif (limits.runtime_minutes > 0
              and now - session.started_at >= timedelta(minutes=limits.runtime_minutes)):
            self._wind_down(session, LIMIT_TIME, now)
        elif (session.system == PROJECTS
              and session.failures_in_a_row >= FAILURES_IN_A_ROW_MAX):
            self._wind_down(session, FAILURE_STOP, now)

    def _wind_down(self, session: Session, reason: str, now: datetime) -> None:
        session.state = WINDING_DOWN
        session.reason = reason
        session.wound_down_at = now
        session.settle_streak = 0
        self._emit(session, WINDING_DOWN)

    def _end(self, session: Session, reason: str, now: datetime) -> None:
        session.state = ENDED
        session.reason = reason
        session.ended_at = now
        session.settle_streak = 0
        self._ended.append(session)
        self._emit(session, ENDED)

    def _emit(self, session: Session, kind: str, *, cleared_hold: bool = False) -> None:
        self._events.append(SessionEvent(
            system=session.system, kind=kind, reason=session.reason,
            initial=session.initial, cleared_hold=cleared_hold,
        ))
        self.dirty = True


def systems_from_start_payload(payload: object) -> dict[str, Limits]:
    """The systems a ``start`` command asks for. ``{"systems": {name: limits}}`` keeps the known
    names; a payload without ``"systems"`` is the shape before the two systems were separable
    (``{"fresh", "max_runtime", "max_proposals"}``) and means interviews with those limits; a
    non-mapping or a malformed ``"systems"`` asks for nothing."""
    if not isinstance(payload, Mapping):
        return {}
    if "systems" not in payload:
        return {INTERVIEWS: Limits(
            count=_non_negative_int(payload.get("max_proposals", 0)),
            runtime_minutes=_non_negative_int(payload.get("max_runtime", 0)),
        )}
    systems = payload["systems"]
    if not isinstance(systems, Mapping):
        return {}
    return {
        name: Limits.from_mapping(value) for name, value in systems.items() if name in SYSTEMS
    }


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _limits_text(system: object, limits: Limits) -> str:
    parts = []
    if limits.count:
        unit = "finished project" if system == PROJECTS else "proposal"
        parts.append(_plural(limits.count, unit))
    if limits.runtime_minutes:
        parts.append(_plural(limits.runtime_minutes, "minute"))
    return ", ".join(parts) or "no limit"


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def describe_records(
    records: Sequence[Mapping], *, run_running: bool = True,
) -> list[dict[str, object]]:
    """One display row per session record (a run's Activity page): ``system``, ``started_at``
    and ``ended_at`` (aware datetimes or None), ``limits`` in words and ``outcome``. Of a run
    that is no longer running (``run_running`` False), a record whose process died before it
    ended shows as the run stopped, as a resume records it (``_closed_record``)."""
    rows: list[dict[str, object]] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        if not run_running:
            record = _closed_record(record)
        system = record.get("system")
        state = record.get("state")
        if state == ENDED:
            outcome = REASON_LABELS.get(str(record.get("reason")), "ended")
        elif state == WINDING_DOWN:
            outcome = "winding down"
        else:
            outcome = "running"
        rows.append({
            "system": SYSTEM_LABELS.get(str(system), str(system)),
            "started_at": _parse_time(record.get("started_at")),
            "ended_at": _parse_time(record.get("ended_at")),
            "limits": _limits_text(system, Limits.from_mapping(record.get("limits"))),
            "outcome": outcome,
        })
    return rows
