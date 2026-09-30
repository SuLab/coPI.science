"""Shared state, run state and wiring primitives for the engine units.

``EngineContext`` is what every unit may read. ``RunState`` is whether the run is
going and why it is ending. ``via`` lets a unit keep a moved method body verbatim
while the attribute it names lives on another unit. The Protocol and callable
types are the five upward calls that become ports instead of a unit reaching back
into the unit above it.

``SimulationEngine`` (``src.agent.simulation``) forwards every member that lives
here or on a unit, so ``engine.<name>`` reads, writes, deletes and monkeypatches
reach the one owner.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from src.agent.end_reasons import stronger_reason
from src.agent.locks import LockRegistry
from src.agent.message_log import MessageLog

if TYPE_CHECKING:
    from src.agent.agent import Agent


class _Via:
    """A class attribute that reads, writes and deletes ``self.<holder>.<name>``.

    A moved method body that says ``self._channel_visibility`` inside, say,
    ``Scheduler`` reads the ``ChannelDirectory``'s attribute through
    ``_channel_visibility = via("_channel_directory")``. Writes and deletes go to
    the owner as well, so every attribute still exists exactly once. The lookup
    happens on every access, so a replacement installed on the owner (a test's
    ``engine._flush_persisted = fake``) is what every unit sees.
    """

    __slots__ = ("holder", "name")

    def __init__(self, holder: str, name: str | None = None) -> None:
        self.holder = holder
        self.name = name

    def __set_name__(self, owner: type, attr: str) -> None:
        if self.name is None:
            self.name = attr

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        return getattr(getattr(obj, self.holder), self.name)

    def __set__(self, obj: Any, value: Any) -> None:
        setattr(getattr(obj, self.holder), self.name, value)

    def __delete__(self, obj: Any) -> None:
        delattr(getattr(obj, self.holder), self.name)


def via(holder: str, name: str | None = None) -> Any:
    """Declare that ``self.<attr>`` means ``self.<holder>.<name or attr>`` (see ``_Via``)."""
    return _Via(holder, name)


class EngineContext:
    """The state every unit may read.

    ``channel_id_resolver`` answers a channel's Slack id by name (``None`` when
    unknown); the orchestrator points it at the channel directory so
    ``Persistence`` never reads the directory's map itself.
    """

    def __init__(
        self,
        *,
        agents: dict[str, Agent],
        slack_clients: dict,
        message_log: MessageLog,
        session_factory: Any,
        simulation_run_id: uuid.UUID | None,
        slack_enabled: bool,
    ) -> None:
        self.agents = agents
        self.slack_clients = slack_clients
        self.message_log = message_log
        self.session_factory = session_factory
        self.simulation_run_id = simulation_run_id
        # When False, the local DB is the sole conversation store and no Slack
        # API calls are made (transports are NullTransport). Drives the roster
        # gate and the DB inbox poller. See specs/local-db-conversations.md.
        self.slack_enabled = slack_enabled

        # Two-lane concurrent scheduler (docs/specs/2026-08-14-two-lane-
        # concurrent-scheduler-design.md §3). Per-key lock registries, keyed
        # by thread_id and agent_id respectively — disjoint namespaces, so a
        # single coroutine holding one can never re-acquire the other under
        # the same key and deadlock on itself. That disjointness does NOT by
        # itself rule out a cross-coroutine deadlock, though: two coroutines
        # acquiring the SAME TWO registries in opposite order still can. The
        # one global rule that prevents it, enforced by convention (no
        # compiler-checked guarantee exists — see
        # test_thread_lock_then_agent_lock_does_not_deadlock_against_an_agent_lock_only_caller
        # and test_no_call_site_bypasses_acquire_all, both in
        # tests/unit/test_reply_lane.py, for the empirical regression):
        #
        #   Acquire the thread lock before the agent lock. Never the reverse.
        #
        # Every call site that takes both follows this: the reply lane
        # (_dispatch_reply_lane's _run) holds a thread lock for the whole
        # servicing span, and everything nested under it that also needs an
        # agent lock (_close_thread, _evict_dead_thread) acquires the agent
        # lock WHILE the thread lock is already held. _phase5_new_post takes
        # only the agent lock and never a thread lock (its own _post_message
        # call never carries a thread_ts, so it can never reach
        # _evict_dead_thread), so it can't invert the order.
        self.thread_locks = LockRegistry()
        self.agent_locks = LockRegistry()
        self.channel_id_resolver: Callable[[str], str | None] = lambda _name: None


class RunState:
    """Whether the run is going, the event that cuts an idle sleep short on a stop,
    and why the run is ending (the end-reason vocabulary and precedence live in
    ``src.agent.end_reasons``)."""

    def __init__(self) -> None:
        self.running = False
        # Set by request_stop() (the signal handler's sync entry point) to both
        # end the main loop and cut short an in-progress idle-backoff sleep, so
        # the final flush happens well inside the container's stop grace period.
        # See _sleep / request_stop (R2).
        self.stop_event = asyncio.Event()
        # Why the run is ending (src/agent/end_reasons.py): set only through
        # request_stop, and read by stop() to choose the shutdown sweep. None
        # means nothing recorded one — stop() then behaves exactly as before.
        self.end_reason: str | None = None

    def request_stop(self, reason: str = "operator") -> None:
        """Ask the main loop to exit — safe to call from a signal handler.

        ``reason`` is one of `src/agent/end_reasons.py`'s vocabulary (a bare call
        means the operator's default Stop). It is recorded before anything else,
        so an unknown reason raises ``ValueError`` and changes nothing; a later
        call can only raise the recorded reason's class.

        Deliberately does no I/O: it only records the reason, flips the flag and
        wakes any in-flight idle-backoff sleep. The flush is done by ``stop()`` on
        the main coroutine's own path (see src/agent/main.py), so it can be
        awaited to completion rather than left in a fire-and-forget task that the
        interpreter may cancel at shutdown (R2).
        """
        self.end_reason = stronger_reason(self.end_reason, reason)
        self.running = False
        self.stop_event.set()


class VerdictLedgerPort(Protocol):
    """What ``Headlines`` needs from the verdict store.

    ``Verdicts`` implements it. The announce ledger itself lives in ``Headlines``
    (``is_announced`` / ``mark_announced``, spec §8.2).
    """

    def patch_pending_summary(self, thread_id: str, posted_at: datetime) -> None: ...

    def assessed_thread_ids(self) -> list[str]: ...


#: ``SlackIO`` calls this when Slack says a thread is gone (was ``_evict_dead_thread``).
ThreadGoneCallback = Callable[[str], Awaitable[None]]
#: ``SlackIO._strip_disallowed_tags``: the roster's outbound cohort tag filter.
TagFilter = Callable[[str | None, "Agent"], tuple[str | None, int]]
#: ``Roster._rejection_counts``: the post lane's live rejection counters.
RejectionCounts = Callable[[], dict[str, int]]
