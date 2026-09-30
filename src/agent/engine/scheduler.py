"""Post-lane pacing: time limit, budget, rate allowance, load and the weighted agent draw (spec §7.1)."""

from __future__ import annotations

import logging
import random
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from src.agent.agent import Agent
from src.agent.engine import deps
from src.agent.engine.constants import _UNSET
from src.agent.engine.context import EngineContext, via
from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE

if TYPE_CHECKING:
    from src.agent.engine.channel_directory import ChannelDirectory

logger = logging.getLogger("src.agent.simulation")


class Scheduler:
    """Whether and which agent may take a post-lane turn."""

    agents = via("ctx")
    message_log = via("ctx")
    _channel_visibility = via("_channel_directory")

    OWNED_STATE: tuple[str, ...] = (
        "max_runtime_minutes",
        "budget_cap",
        "_start_time",
        "_role_rate_cache",
    )

    def __init__(
        self,
        ctx: EngineContext,
        *,
        channel_directory: ChannelDirectory,
        max_runtime_minutes: int,
        budget_cap: int,
    ) -> None:
        self.ctx = ctx
        self._channel_directory = channel_directory
        self.max_runtime_minutes = max_runtime_minutes
        self.budget_cap = budget_cap

        # role name -> calls_per_load_per_window override (or None). See _calls_per_load.
        self._role_rate_cache: dict[str, int | None] = {}

        self._start_time: datetime | None = None

    @property
    def is_within_time_limit(self) -> bool:
        if self.max_runtime_minutes <= 0:
            return True  # run forever (until SIGTERM)
        if not self._start_time:
            return True
        elapsed = (deps.datetime.now(UTC) - self._start_time).total_seconds()
        return elapsed < self.max_runtime_minutes * 60

    def _agent_within_budget(self, agent: Agent) -> bool:
        if self.budget_cap <= 0:
            return True  # unlimited
        return agent.api_call_count < self.budget_cap

    def _agent_load(self, agent: Agent) -> int:
        """Concurrent conversational obligations for one agent.

        The shared signal behind BOTH the rate allowance (``_within_rate_limit``)
        and the selection weight (``_select_agent``). Deriving both from one
        number is the point: the failure this fixes was the limiter and the
        scheduler holding contradictory views of what a hub deserves — the
        reactive tier gave the blackbird hub a 7x boost while the cumulative cap
        benched it for 161 consecutive turns, and the cap won, silently. See
        docs/specs/2026-08-06-hub-budget-scheduler-design.md §1.4.

        Floors at 1 so an idle agent stays eligible. Ceilings at
        ``active_thread_threshold`` so nothing can inflate its own allowance past
        the thread cap it is already bound by — that clamp is what stops a
        thread-opening runaway from financing itself (§4.1).
        """
        live = sum(
            1 for t in agent.state.active_threads.values() if t.status == "active"
        )
        return max(1, min(live, deps.get_settings().active_thread_threshold))

    def _calls_per_load(self, agent: Agent) -> int:
        """Per-unit-of-load LLM allowance for this agent's role.

        Cached by role NAME, so an agent flipping roles at runtime simply looks
        up a different key and needs no invalidation. The only staleness is a
        role.toml edited mid-run, which matches get_settings() already being
        lru_cached — both need a container recreate (design §5).

        The cache exists because load_role() reads TOML from disk on every call
        and this runs for every agent on every scheduler tick.
        """
        cached = self._role_rate_cache.get(agent.role, _UNSET)
        if cached is _UNSET:
            cached = deps.load_role(agent.role).calls_per_load_per_window
            self._role_rate_cache[agent.role] = cached
        if cached is not None:
            return cached
        return deps.get_settings().llm_calls_per_load_per_window

    def _allowance_for(self, agent: Agent) -> int:
        """Window allowance for one agent. The hub is on its own ceiling.

        A ``scout_hub`` sits on an unpaced lane (the reservation reply path
        of Task 9 of docs/plans/2026-08-14-two-lane-concurrent-scheduler.md
        fires without the per-turn fan-out cap re-checking it), so the
        per-load allowance that bounds every ``pi_lab`` no longer applies to
        it — it gets ``hub_llm_calls_per_window`` instead, a brake against
        runaway rather than a load-scaled budget. Every other role keeps the
        existing formula. Shared by both ``_within_rate_limit`` (selection)
        and ``Agent.try_reserve`` (spend) so the two checks cannot disagree
        about what the hub deserves — that disagreement is exactly what
        benched the hub for 161 turns in run 4f1e8395.
        """
        settings = deps.get_settings()
        if agent.role == "scout_hub":
            return settings.hub_llm_calls_per_window
        return self._calls_per_load(agent) * self._agent_load(agent)

    def _within_rate_limit(self, agent: Agent, now: float) -> bool:
        """Sliding-window LLM rate check — the LIVE throttle.

        allowance = ``self._allowance_for(agent)``: ``_calls_per_load(agent) *
        _agent_load(agent)`` for a pi_lab, or ``hub_llm_calls_per_window`` for
        the scout_hub, over llm_rate_window_seconds. Unlike the cumulative cap
        this replaces, it self-heals: entries age out, so an agent throttled
        now is eligible later. See design §4.2, §5.

        This is the SELECTION-time check (consulted by ``_turn_eligible``).
        The SPEND-time check is ``Agent.try_reserve``, called immediately
        before each LLM call in ``_reply_to_thread`` / ``_phase5_new_post`` —
        a selection-time-only check cannot bound concurrent spend once several
        calls are in flight for one agent.
        """
        allowance = self._allowance_for(agent)
        window_start = now - deps.get_settings().llm_rate_window_seconds
        times = agent.state.call_times
        while times and times[0] < window_start:
            times.popleft()
        ok = len(times) < allowance
        if not ok and not agent.state.throttled:
            logger.warning(
                "[%s] throttled: %d LLM calls in the last %ds (allowance %d). "
                "Eligible again as the window slides.",
                agent.agent_id, len(times),
                deps.get_settings().llm_rate_window_seconds, allowance,
            )
        agent.state.throttled = not ok
        return ok

    def _active_thread_count(self, agent: Agent) -> int:
        """Count this agent's active threads."""
        return len(agent.state.active_threads)

    def _count_today_posts(self, agent: Agent) -> int:
        """Count top-level posts by this agent in public channels, in the current Pacific time day.

        collab_private channels are flat (every refinement reply is a top-level
        post) and also host PI-initiated handover messages under the bot's
        token. Both are legitimate per the 2-party private-channel design, so
        they must not consume the spam-prevention cap that's scoped to public
        new-conversation posts.
        """
        from zoneinfo import ZoneInfo
        pacific = ZoneInfo("America/Los_Angeles")
        today_start = deps.datetime.now(pacific).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).timestamp()
        return sum(
            1 for e in self.message_log.get_agent_top_level_posts(agent.agent_id, limit=100)
            if e.posted_at >= today_start
            and self._channel_visibility.get(e.channel) != VISIBILITY_COLLAB_PRIVATE
        )

    def _turn_eligible(self, agent: Agent, now: float) -> bool:
        """Selection eligibility for one agent.

        - within the LEGACY cumulative cap. Inert by default (``budget_cap``
          defaults to 0, and ``_agent_within_budget`` short-circuits at <= 0);
          armed only when an operator passes ``--budget``. Retained, not removed,
          for back-compat — see design §6;
        - within its sliding-window rate limit. This is the live throttle;
        - past its per-agent cooldown. ``turn_delay_seconds`` throttles an
          individual agent's tempo; enforcing it here (rather than as a global
          ``asyncio.sleep`` after every productive turn) leaves the rest of the
          roster free to act while one agent sits out. See v2 §10.3.
        - not already ``in_flight``: a post-lane turn for this agent is
          currently running. A no-op today (the post lane is strictly
          sequential — the previous turn always finishes before the next
          selection), but load-bearing once loop iterations can overlap.
        """
        if agent.state.in_flight:
            return False
        if not self._agent_within_budget(agent):
            # Ordering is deliberate and must not change: the legacy cap decides
            # eligibility first (that is what keeps the --budget compat tests
            # meaningful). But short-circuiting here also froze
            # ``state.throttled``, so with --budget armed an agent's next genuine
            # throttle transition logged nothing. Evaluate the window check for
            # its SIDE EFFECT (expire old entries, refresh the flag, emit the
            # one-shot warning) and discard the result — eligibility is still
            # decided by the cap alone.
            self._within_rate_limit(agent, now)
            return False
        if not self._within_rate_limit(agent, now):
            return False
        delay = deps.get_settings().turn_delay_seconds
        if delay > 0 and (now - agent.state.last_selected) < delay:
            return False
        return True

    def _select_agent(self) -> Agent | None:
        """Select the next agent for a post-lane turn (sequential — one at a time).

        Staleness-weighted random, scaled by load:
        P(agent) ∝ (now - last_selected) * _agent_load(agent), with a penalty
        for agents that have repeatedly skipped Phase 5
        (weight /= 2^(skips-2) once skips >= 3). The load factor is what makes
        a star's hub — one endpoint of every conversation — draw a share that
        tracks the edges it actually sits on, instead of the 1/N a uniform
        weighting gave it. See design §4.3.

        There is no reactive tier here any more: replies leave the paced pool
        entirely (see `_dispatch_reply_lane`), so this is pure proactive
        selection over the eligibility pool (`_turn_eligible`) — budget, the
        sliding-window rate limit, the per-agent `turn_delay_seconds`
        cooldown, and not already `in_flight`.
        """
        now = deps.time.time()
        candidates = [a for a in self.agents.values() if self._turn_eligible(a, now)]
        if not candidates:
            return None

        weights = []
        for a in candidates:
            w = max(now - a.state.last_selected, 1.0) * self._agent_load(a)
            skips = a.state.consecutive_phase5_skips
            if skips >= 3:
                w /= 2 ** (skips - 2)
            weights.append(w)
        return random.choices(candidates, weights=weights, k=1)[0]
