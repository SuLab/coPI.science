"""The post lane: channel discovery, thread activation, the Phase-5 pitch and its post-type rules, and the proposal target (spec §7.1)."""

from __future__ import annotations

import json
import logging
import random
import re
import uuid
from typing import TYPE_CHECKING

from src.agent.agent import Agent
from src.agent.engine import constants, deps
from src.agent.engine.constants import PROPOSAL_DRAIN_SETTLE_TICKS
from src.agent.engine.context import EngineContext, via
from src.agent.engine.helpers import _was_truncated
from src.agent.engine.sidecar import _strip_assessment_sidecar
from src.agent.persona_sections import match_channels
from src.agent.post_types import PostTypeSpec, available_for, eligible_targets, render_menu
from src.agent.role_capabilities import capabilities_for, hub_role_names
from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE

if TYPE_CHECKING:
    from src.agent.engine.channel_directory import ChannelDirectory
    from src.agent.engine.llm_log import LlmLog
    from src.agent.engine.panel import Panel
    from src.agent.engine.roster import Roster
    from src.agent.engine.scheduler import Scheduler
    from src.agent.engine.slack_io import SlackIO
    from src.agent.engine.threads import Threads

logger = logging.getLogger("src.agent.simulation")


def desired_channels(agent: Agent) -> set[str]:
    """The channels ``agent`` should be subscribed to now (spec 2026-10-05 §6.5, D28): every
    ``SEEDED_CHANNELS`` entry for a hub role; otherwise ``_UNIVERSAL_CHANNELS`` plus each
    ``_CHANNEL_KEYWORDS`` channel matched against the persona's tag sections
    (``persona_sections.match_channels``). Reads all three as ``constants.X``."""
    if agent.role in hub_role_names():
        return set(constants.SEEDED_CHANNELS)
    return set(constants._UNIVERSAL_CHANNELS) | match_channels(
        agent.public_profile, constants._CHANNEL_KEYWORDS
    )


class PostLane:
    """The paced post lane and everything that decides what it may post."""

    _agent_locks = via("ctx", "agent_locks")
    agents = via("ctx")
    message_log = via("ctx")
    session_factory = via("ctx")
    simulation_run_id = via("ctx")
    slack_clients = via("ctx")
    _channel_id_map = via("_channel_directory")
    _channel_visibility = via("_channel_directory")
    _llm_log_buffer = via("_llm_log")
    _specialist_consults = via("_panel")
    _bot_name_to_id = via("_roster")
    _infer_agent_id = via("_roster")
    _strip_disallowed_tags = via("_roster")
    _active_thread_count = via("_scheduler")
    _allowance_for = via("_scheduler")
    _count_today_posts = via("_scheduler")
    _post_message = via("_slack_io")
    _closed_thread_ids = via("_threads")
    _get_prior_threads_for_agent = via("_threads")

    OWNED_STATE: tuple[str, ...] = ("max_proposals", "_proposals_posted", "_proposal_drain_streak", "_role_post_types_cache", "_post_type_rejections")

    def __init__(
        self,
        ctx: EngineContext,
        *,
        scheduler: Scheduler,
        roster: Roster,
        threads: Threads,
        slack_io: SlackIO,
        llm_log: LlmLog,
        channel_directory: ChannelDirectory,
        panel: Panel,
        max_proposals: int,
    ) -> None:
        self.ctx = ctx
        self._scheduler = scheduler
        self._roster = roster
        self._threads = threads
        self._slack_io = slack_io
        self._llm_log = llm_log
        self._channel_directory = channel_directory
        self._panel = panel
        # 0 = off. When >0, the engine stops opening NEW pitches once this many
        # top-level posts exist this run, then ends the run when every opened
        # interview has drained (see _proposal_target_drained). Rehydrated on
        # resume from agent_messages (phase='new_post', see
        # _rehydrate_proposal_count) — but rehydration only restores the
        # COUNT; the cap itself is whatever the caller passes in here. main.py's
        # _run_simulation inherits max_proposals from the resumed run's stored
        # config when the CLI value is 0, which is what actually keeps a resume
        # from silently disabling the gate. The gate only enforces when
        # max_proposals > 0.
        self.max_proposals = max_proposals
        self._proposals_posted = 0
        self._proposal_drain_streak = 0

        # role name -> declared post_types. Same reason as the scheduler's
        # _role_rate_cache: load_role() hits the disk on every call.
        self._role_post_types_cache: dict[str, tuple[PostTypeSpec, ...]] = {}

        # Per-agent count of new-post rejections from _post_type_rejection —
        # unavailable post_type, missing/unreachable tagged_agent, or a
        # mutilated-mention reject. Mirrors the roster's _cohort_tags_stripped: a
        # deployment where every pitch is rejected on (e.g.) a tagged_agent
        # spelling slip is otherwise only visible by grepping logs.
        self._post_type_rejections: dict[str, int] = {}

    async def _run_post_turn(self, agent: Agent) -> bool:
        """Run Phase 1 + Phase 5 for one agent. Returns True if work was done.

        The paced lane: Phase 3 (thread activation) and Phase 4 (thread
        reply) moved to the reply lane (`_dispatch_reply_lane` /
        `_service_reply`) entirely — see docs/specs/2026-08-14-two-lane-
        concurrent-scheduler-design.md §2.

        This function does NOT touch
        `last_seen_cursor` at all — neither Phase 1 nor Phase 5 reads it, and
        `_dispatch_reply_lane` already owns cursor advancement for every
        agent, every tick (that is the only reader: `_phase3_activate_threads`
        / `_pending_reply_pairs`'s `has_new_reply_from_other` check). A
        second writer here, taking its OWN snapshot after `_dispatch_reply_
        lane` has already run (and possibly spent many seconds/minutes making
        LLM calls that posted new messages), would capture a `latest_
        timestamp` far ahead of what this agent's Phase 3 actually saw this
        tick — and assigning it would silently mark a message Phase 3 never
        processed as "seen", permanently stalling that thread with no
        backstop (`has_pending_reply` was already cleared by the reply that
        made the thread's status update). This is exactly the Task-6 bug,
        reintroduced by a second cursor writer with no corresponding reader.
        See tests/unit/test_cursor_advance.py.
        """
        settings = deps.get_settings()
        api_calls_before = agent.api_call_count
        agent.state.in_flight = True
        try:
            # Phase 1: Channel discovery
            await self._phase1_channel_discovery(agent)

            # Spontaneous post timer — allow one Phase 5 call after enough
            # idle time so agents can organically start new conversations. This
            # is the ONLY Phase 5 gate here: the paced post lane must not be
            # driven by reply volume from the unpaced reply lane — see
            # tests/unit/test_post_lane.py. The daily cap and the other Phase 5
            # guards are unchanged and live inside `_phase5_new_post` itself.
            base_interval = settings.phase5_spontaneous_interval * 60  # to seconds
            skips = agent.state.consecutive_phase5_skips
            stretch = min(
                max(skips, 1), settings.phase5_spontaneous_interval_max_multiplier
            )
            spontaneous_interval = base_interval * stretch
            since_last_action = deps.time.time() - agent.state.last_phase5_action_time
            spontaneous_ready = since_last_action >= spontaneous_interval

            if spontaneous_ready:
                await self._phase5_new_post(agent)
            else:
                logger.debug(
                    "[%s] Phase 5: Skipped (spontaneous timer not due for %ds)",
                    agent.agent_id,
                    int(spontaneous_interval - since_last_action),
                )

            return agent.api_call_count > api_calls_before
        finally:
            agent.state.in_flight = False

    async def _phase1_channel_discovery(self, agent: Agent) -> None:
        """Recompute the agent's channels from its current persona and apply the change:
        join each new channel in Slack and, only on success, add it to
        ``subscribed_channels``; drop each no-longer-matched channel from
        ``subscribed_channels`` only. Slack membership is kept (D50), but a dropped
        channel no longer feeds activation, which reads ``subscribed_channels``."""
        desired = desired_channels(agent)
        subscribed = agent.state.subscribed_channels
        new_channels = desired - subscribed
        dropped = subscribed - desired
        if new_channels:
            joined = set()
            for ch_name in new_channels:
                ch_id = self._channel_id_map.get(ch_name)
                if ch_id:
                    client = self.slack_clients.get(agent.agent_id)
                    if client and await client.ajoin_channel(ch_id):
                        subscribed.add(ch_name)
                        joined.add(ch_name)
            if joined:
                logger.info("[%s] Phase 1: Joined channels: %s", agent.agent_id, joined)
        if dropped:
            subscribed.difference_update(dropped)
            logger.info(
                "[%s] Phase 1: no longer matched, unsubscribed (Slack membership kept): %s",
                agent.agent_id, dropped,
            )

    def _phase3_activate_threads(self, agent: Agent) -> None:
        """
        Auto-activate threads where this agent was tagged or
        where someone replied to this agent's top-level posts.

        Skipped entirely for entries in collab_private channels: those channels
        are flat discussions (no threading), so tags and replies there are
        just conversation content for later phases to read directly, not
        thread-activation signals.

        Human-authored (``is_bot=False``) entries are skipped in all three loops
        below (tags, replies, hub auto-activation) — the bot-behavior half of
        decision 5 (2026-08-12 PI-interaction removal cycle): there is no
        PI-bot interaction surface left for a human post to activate a thread,
        including the substring-match trap ``_infer_agent_id`` could otherwise
        walk into (e.g. a human sender name like "Andrew Su (PI)" contains the
        real agent_id "su"). The GATED ``MessageLog`` reads these loops consume
        (``get_tags_for_agent``/``get_replies_to_agent_posts``/
        ``get_new_top_level_posts``) deliberately still return human rows —
        they are general-purpose per-agent reads whose history/observability
        half of decision 5 is kept — so the filter belongs here, at the actual
        point of activation, not in those shared methods.
        """
        cursor = agent.state.last_seen_cursor

        # Check for tags
        tagged_entries = self.message_log.get_tags_for_agent(
            agent.bot_name, cursor, allowed_sender_ids=agent.allowed_sender_ids
        )
        for entry in tagged_entries:
            if not entry.is_bot:
                continue
            # Private channels are flat — no thread activation.
            if self._channel_visibility.get(entry.channel) == VISIBILITY_COLLAB_PRIVATE:
                continue
            thread_id = entry.thread_ts or entry.ts
            if thread_id in agent.state.active_threads:
                continue
            if thread_id in self._closed_thread_ids:
                continue
            # Threshold gates Phase 5 (starting new threads), not Phase 3.
            # Ignoring an explicit @-mention is worse than running over the cap.
            # Check thread participation rules
            allowed = self.message_log.get_thread_allowed_agents(thread_id)
            if allowed and agent.agent_id not in allowed:
                logger.info(
                    "[%s] Phase 3: Skipping tagged thread %s — not in allowed set %s",
                    agent.agent_id, thread_id, allowed,
                )
                continue
            # Determine the other agent
            other_id = self._infer_agent_id(entry.sender_name) or entry.sender_agent_id
            if other_id and other_id != agent.agent_id:
                self._threads.activate_thread(
                    agent, thread_id,
                    channel=entry.channel,
                    other_agent_id=other_id,
                    message_count=self.message_log.get_thread_message_count(thread_id),
                    has_pending_reply=True,
                    # Initial seed for the monotonic latch — see
                    # ThreadState.floor_armed and the latch at the top of
                    # _reply_to_thread.
                    floor_armed=bool(self._specialist_consults),
                )
                logger.info(
                    "[%s] Phase 3: Activated thread %s (tagged by %s)",
                    agent.agent_id, thread_id, other_id,
                )

        # Check for replies to agent's own top-level posts
        reply_entries = self.message_log.get_replies_to_agent_posts(
            agent.agent_id, cursor, allowed_sender_ids=agent.allowed_sender_ids
        )
        for entry in reply_entries:
            if not entry.is_bot:
                continue
            # Private channels are flat — no thread activation.
            if self._channel_visibility.get(entry.channel) == VISIBILITY_COLLAB_PRIVATE:
                continue
            thread_id = entry.thread_ts
            if not thread_id or thread_id in agent.state.active_threads:
                continue
            if thread_id in self._closed_thread_ids:
                continue
            # Threshold gates Phase 5 (starting new threads), not Phase 3.
            # Ghosting a reply to our own post is worse than running over the cap.
            # Check thread participation rules
            allowed = self.message_log.get_thread_allowed_agents(thread_id)
            if allowed and len(allowed) >= 2 and agent.agent_id not in allowed:
                continue
            other_id = self._infer_agent_id(entry.sender_name) or entry.sender_agent_id
            if other_id and other_id != agent.agent_id:
                self._threads.activate_thread(
                    agent, thread_id,
                    channel=entry.channel,
                    other_agent_id=other_id,
                    message_count=self.message_log.get_thread_message_count(thread_id),
                    has_pending_reply=True,
                    # Initial seed for the monotonic latch — see
                    # ThreadState.floor_armed and the latch at the top of
                    # _reply_to_thread.
                    floor_armed=bool(self._specialist_consults),
                )
                logger.info(
                    "[%s] Phase 3: Activated thread %s (reply from %s)",
                    agent.agent_id, thread_id, other_id,
                )

        # Hub auto-activation: the scout hub opens an interview thread on
        # every new lab top-level post, no @-mention required. Gated on the
        # plain `agent.role` attribute (NOT `self._roles_by_agent()` — see
        # INV-E structural note 4, a separate, separately-recomputed
        # consumer of role knowledge).
        caps = capabilities_for(agent.role)
        if caps is not None and caps.auto_activates_on_lab_posts:
            self._auto_activate_lab_posts(agent, since=cursor)

    def _auto_activate_lab_posts(self, agent: Agent, since: float) -> int:
        """Open an interview thread on every new lab top-level post after ``since``.

        The hub's half of Phase 3, extracted so the resume one-shot
        (`_recover_reply_less_pitches`) applies exactly the same gates: human
        rows, the agent's cohort gate (`allowed_sender_ids`), collab_private
        channels, closed threads, already-active threads and
        `get_thread_allowed_agents`. Returns how many threads it activated.
        The caller decides which agents are hubs.
        """
        activated = 0
        new_posts = self.message_log.get_new_top_level_posts(
            since=since,
            channels=agent.state.subscribed_channels,
            exclude_agent_id=agent.agent_id,
            allowed_sender_ids=agent.allowed_sender_ids,
        )
        for entry in new_posts:
            if not entry.is_bot:
                continue
            # Private channels are flat — no thread activation.
            if self._channel_visibility.get(entry.channel) == VISIBILITY_COLLAB_PRIVATE:
                continue
            thread_id = entry.thread_ts or entry.ts
            if thread_id in agent.state.active_threads:
                continue
            if thread_id in self._closed_thread_ids:
                continue
            # Check thread participation rules
            allowed = self.message_log.get_thread_allowed_agents(thread_id)
            if allowed and agent.agent_id not in allowed:
                continue
            other_id = self._infer_agent_id(entry.sender_name) or entry.sender_agent_id
            if other_id and other_id != agent.agent_id:
                self._threads.activate_thread(
                    agent, thread_id,
                    channel=entry.channel,
                    other_agent_id=other_id,
                    message_count=self.message_log.get_thread_message_count(thread_id),
                    has_pending_reply=True,
                    # Initial seed for the monotonic latch — see
                    # ThreadState.floor_armed and the latch at the top of
                    # _reply_to_thread.
                    floor_armed=bool(self._specialist_consults),
                )
                activated += 1
                logger.info(
                    "[%s] Phase 3: Auto-activated interview thread %s (lab post by %s)",
                    agent.agent_id, thread_id, other_id,
                )
        return activated

    async def _phase5_new_post(self, agent: Agent) -> None:
        """Optionally start a new top-level thread.

        Hard-gated for scout_hub (decision 9, reply-only-hub reconciliation):
        the hub's former standalone :mag: Opportunity Assessment is now the
        `<assessment_json>` sidecar carried inside its own Phase-4 CONCLUDE
        reply instead (see `_reply_to_thread`) — it has no top-level post
        type left, ever (role.toml declares `post_types = []`, belt-and-
        suspenders). Returning here before ANY work — no settings lookup, no
        prompt built, no LLM call — is what stops a permanently empty menu
        from burning a full-price Opus call every single turn just to be
        told "skip" (measured cost/noise trap: one production run took the
        hub 30 turns and 0 useful phase-5 LLM calls). Gated on role, not on
        an empty menu, so the invariant holds even if role.toml were ever
        misconfigured back to declaring something.
        """
        caps = capabilities_for(agent.role)
        if caps is None or not caps.posts_new_threads:
            return

        # Serialises this agent's Phase-5 turn against the reply lane's
        # _close_thread / _evict_dead_thread mutations of THIS agent's
        # active_threads (spec §4.4/§4.5) — both read by
        # _active_thread_count/_count_today_posts above, and by the daily-cap
        # / thread-threshold checks just below. Single-key acquisition, so
        # ordering relative to _close_thread/_evict_dead_thread's (possibly
        # multi-key) acquisitions is irrelevant here — acquire_all sorts
        # regardless. This never nests a THREAD lock inside it: the only
        # _post_message call below carries no thread_ts, so it can never
        # raise ThreadNotFound / reach _evict_dead_thread — see the ordering
        # note on _thread_locks in __init__.
        async with self._agent_locks.acquire_all(agent.agent_id):
            settings = deps.get_settings()

            # Stamp the spontaneous-post timer up front: consulting Phase 5 consumes
            # the opportunity regardless of whether we end up posting, skipping, or
            # bailing out early. Without this, a "skip" leaves the timer stale and
            # every subsequent turn re-fires Phase 5, burning an LLM call per turn.
            agent.state.last_phase5_action_time = deps.time.time()

            # Daily post cap — pi_lab is capped to one pitch per day (design §9).
            # scout_hub never reaches this line (hard-gated above), and it is the
            # only other role, so `lab_daily_post_cap` is unconditional here — the
            # generic `daily_post_cap` setting this once ternaried against was
            # unreachable and was deleted (2026-08-12 release-gating fix pass, M1).
            today_posts = self._count_today_posts(agent)
            cap = settings.lab_daily_post_cap
            if today_posts >= cap:
                logger.debug("[%s] Phase 5: Skipped (daily cap %d/%d)", agent.agent_id, today_posts, cap)
                return

            # Proposal-count limit: once the run has posted its target number of
            # pitches, stop opening new ones and let interviews drain. Checked
            # here, beside the daily cap, so it costs no LLM call.
            if self.max_proposals > 0 and self._proposals_posted >= self.max_proposals:
                logger.info(
                    "[%s] Phase 5: Skipped (proposal cap %d/%d reached)",
                    agent.agent_id, self._proposals_posted, self.max_proposals,
                )
                return

            # Backpressure against STARTING more work than the agent can finish:
            # too many threads open at once. This used to have a second clause
            # (too many of the agent's proposals awaiting web review) and an
            # exemption letting a blocked agent still file one *terminal*
            # artifact past the block — the hub's assessment. Both are gone: the
            # reconciliation deleted the only post type that was ever exempt (see
            # post_types.py), and nothing on this branch creates a new proposal
            # for a PI to review anymore, so there is nothing left to gate on
            # either. A blocked agent (only ever pi_lab in practice — scout_hub
            # is gated above) now has nothing left it could post regardless, so
            # it skips outright here, no LLM call, exactly like the daily cap.
            if self._active_thread_count(agent) >= settings.active_thread_threshold:
                logger.debug(
                    "[%s] Phase 5: Skipped (at/over active_thread_threshold)",
                    agent.agent_id,
                )
                return

            if random.random() < settings.phase5_skip_probability:
                logger.debug("[%s] Phase 5: Skipped (random)", agent.agent_id)
                return

            # Build prompt — include agent's recent posts for dedup
            recent_entries = self.message_log.get_agent_top_level_posts(agent.agent_id, limit=10)
            recent_posts = [
                {"channel": e.channel, "content_snippet": e.content[:150]}
                for e in recent_entries
            ]

            # Phase 5 always operates in a public channel (see build_phase5_prompt's
            # docstring) — there is no longer any per-turn state that could put it in
            # a private-channel context, so prior-threads dedup uses the default
            # (public) visibility.
            prior_threads = self._get_prior_threads_for_agent(agent.agent_id)

            available_types = self._available_post_types(agent)
            if not available_types:
                # Nothing satisfies role ∩ topology — either a misconfigured
                # role.toml or a cohort gate that leaves this agent with no
                # reachable counterparty for anything it declares. This point is
                # only ever reached by an UNBLOCKED agent (a blocked one already
                # returned above), so an empty menu here is always worth a
                # WARNING — there is no longer a quiet/expected empty-menu case
                # to distinguish it from (that was the hub's, and the hub never
                # reaches this line).
                logger.warning(
                    "[%s] Phase 5: no post type satisfiable — check cohort/roster "
                    "for role %r", agent.agent_id, agent.role,
                )
            post_type_menu = render_menu(
                available_types,
                gate=agent.allowed_sender_ids,
                roles_by_agent=self._roles_by_agent(),
                self_id=agent.agent_id,
                bot_names={aid: a.bot_name for aid, a in self.agents.items()},
            )

            system_prompt, messages = agent.build_phase5_prompt(
                recent_posts=recent_posts,
                prior_threads=prior_threads,
                post_type_menu=post_type_menu,
            )

            # Correlation id for this specific call's log row, generated BEFORE the
            # call and carried through log_meta. `channel` isn't known until the
            # model's response is parsed below, so it can't go in log_meta up
            # front — but the row this call appends to the shared
            # `_llm_log_buffer` can be found again afterward by this id, without
            # trusting the buffer's tail (see the retroactive-channel comment
            # below for why position is unsafe under concurrency).
            llm_call_id = uuid.uuid4().hex

            if not agent.try_reserve(
                self._allowance_for(agent), deps.get_settings().llm_rate_window_seconds
            ):
                logger.warning(
                    "[%s] rate-limited; deferring this post", agent.agent_id,
                )
                return
            # already_reserved=True: try_reserve just appended this exact call to
            # call_times — appending again here would double-book it.
            agent.record_api_call(already_reserved=True)
            # See `_was_truncated`; same collection idiom as the Phase-4 site.
            stop_reasons: list[str] = []
            try:
                response = await deps.generate_agent_response(
                    system_prompt=system_prompt,
                    messages=messages,
                    model=settings.llm_agent_model_opus,
                    # Historical sizing note: this used to also cover scout_hub's
                    # opportunity-assessment post here (an 11-section body plus a
                    # ~15-line <assessment_json> sidecar emitted LAST, where 1000
                    # — sized for a short reply/skip decision — truncated the
                    # verdict first while leaving the Slack post looking
                    # complete, F8). The hub is hard-gated out of this function
                    # now (see the docstring) and its assessment moved to the
                    # Phase-4 CONCLUDE reply's own budget instead, so this
                    # function's only caller today (pi_lab) never needs anywhere
                    # near 2500 tokens for a pitch or a skip — kept at this size
                    # anyway rather than re-tuned down, since a smaller ceiling
                    # buys nothing but risk here. NOTE: src/services/llm.py's
                    # retry-at-2x path logs loudly (logger.error) if the retry
                    # ALSO truncates, but it does not retry again.
                    # 3300, up from 2500: the 4.7-generation tokenizer (Opus 5 /
                    # Sonnet 5) yields ~30% more tokens for the same text, so a
                    # ceiling tuned on Sonnet 4.6 truncates sooner. Thinking is
                    # disabled on this path (llm.py's default), so only the
                    # tokenizer change is being compensated for here.
                    max_tokens=3300,
                    log_meta={
                        "agent_id": agent.agent_id,
                        "phase": "new_post",
                        "call_id": llm_call_id,
                    },
                    on_retry=agent.record_api_call,
                    on_stop_reason=stop_reasons.append,
                )
                await self._handle_phase5_response(
                    agent, response, stop_reasons, llm_call_id, available_types,
                )
            except Exception as exc:
                logger.error("[%s] Phase 5 failed: %s", agent.agent_id, exc)

    async def _handle_phase5_response(
        self,
        agent: Agent,
        response: str,
        stop_reasons: list[str],
        llm_call_id: str,
        available_types: tuple[PostTypeSpec, ...],
    ) -> None:
        """Act on one Phase-5 model response: skip a truncated, empty or unparseable
        one, honour a skip, or validate and post the pitch. Called inside
        ``_phase5_new_post``'s ``try`` and agent lock, so its exceptions and its
        early returns end the turn exactly as they did inline."""
        if _was_truncated(stop_reasons):
            # SKIPPED, unlike the Phase-4 reply above, and the asymmetry
            # is the point: nothing is waiting on this. No thread is open,
            # no PI is mid-sentence, and no later turn is owed anything —
            # so a pitch the model did not finish is simply not made. A
            # half-written action envelope is also the shape most likely
            # to parse into a post nobody meant (`_parse_phase5_response`
            # sees a truncated JSON block), which is a workspace-visible
            # artifact that cannot be retracted.
            logger.warning(
                "[%s] Phase 5: response was TRUNCATED (%s) — skipping "
                "the post rather than publishing a half-written one",
                agent.agent_id, ", ".join(stop_reasons) or "?",
            )
            return
        if not response or not response.strip():
            logger.warning("[%s] Phase 5: Empty response from LLM, skipping", agent.agent_id)
            return

        # Parse the JSON + message from the response
        action_data, message_text = self._parse_phase5_response(response)
        if not action_data:
            logger.warning("[%s] Phase 5: Could not parse response", agent.agent_id)
            return

        # A missing `action` is an unparseable response, not a license to
        # post something anyway — defaulting to "new_post" here is exactly
        # what let a hijacked action dict (see _parse_phase5_response's
        # sidecar-strip fix) fall through into posting to #general with an
        # empty post_type instead of being rejected outright.
        action = action_data.get("action")
        if not action:
            logger.warning(
                "[%s] Phase 5: parsed JSON had no 'action' field — "
                "treating as unparseable",
                agent.agent_id,
            )
            return

        if action == "skip":
            agent.state.consecutive_phase5_skips += 1
            logger.info(
                "[%s] Phase 5: Agent chose to skip (streak: %d)",
                agent.agent_id, agent.state.consecutive_phase5_skips,
            )
            return

        if not message_text:
            logger.warning("[%s] Phase 5: No message text in response", agent.agent_id)
            return

        # Real action — reset skip backoff. Capture the pre-reset value
        # first: several rejection paths below need the TRUE streak, not
        # the just-reset 0, to feed _select_agent's damping
        # (`skips >= 3`). Every remaining rejection path (unsupported
        # action, post-type rejection, body-mention rejection) restores it
        # correctly via `previous_skips + 1`.
        previous_skips = agent.state.consecutive_phase5_skips
        agent.state.consecutive_phase5_skips = 0
        agent.state.last_phase5_action_time = deps.time.time()

        channel = action_data.get("channel", "general").lstrip("#")
        post_type = action_data.get("post_type", "")

        # Retroactively add channel to the LLM log entry (unknown at call
        # time). Found by `llm_call_id`, NOT by buffer position — under
        # concurrency (two agents' Phase-5 turns interleaved on the same
        # event loop), another agent's own `_on_llm_call` can append its
        # row to this SHARED buffer in the gap between this call
        # returning and this line running, so `_llm_log_buffer[-1]` is
        # not reliably this call's row. Scanning from the tail is just an
        # optimization (our own row, being the most recent thing we
        # appended, is usually near the end); if it was already flushed
        # to the DB before we got here, skip silently — the row is still
        # a valid log entry without `channel`, and there's nothing to
        # retry.
        for _entry in reversed(self._llm_log_buffer):
            if _entry.get("call_id") == llm_call_id:
                _entry["channel"] = channel
                break

        # Cross-cohort mention stripping now happens in _post_message, which
        # covers every outbound path instead of only this one. Phase 5 still
        # needs the *cleaned* text locally, though: the tagged_agent decision
        # below reads message_text.
        #
        # The JSON `post_type`/`tagged_agent` pair is not the only place a
        # disallowed mention can hide — a spoke can also name an
        # unreachable lab in PROSE with tagged_agent left null, which
        # layers 1-3 below wave through (a broadcast type addresses no one
        # by declaration). Recording whether THIS strip actually removed
        # something lets the new-post branch reject that case instead of
        # publishing a body with the mention silently deleted out from
        # under it — see the mutilation check below.
        #
        # This reads the count _strip_disallowed_tags returns for THIS call,
        # not a before/after delta on the shared self._cohort_tags_stripped
        # counter — under the two-lane scheduler another agent's concurrent
        # post can bump that counter between the "before" and "after" reads,
        # which used to make Phase 5 reject a perfectly clean post.
        message_text, this_call_stripped = self._strip_disallowed_tags(
            message_text, agent
        )
        body_mention_was_stripped = this_call_stripped > 0

        if action != "new_post":
            logger.info(
                "[%s] Phase 5: unsupported action %r — skipping",
                agent.agent_id, action,
            )
            agent.state.consecutive_phase5_skips = previous_skips + 1
            return

        # New top-level post. Layers 1-3, against the SAME set that was
        # rendered into the prompt above. Reject rather than strip-and-
        # publish: a mention stripped out of an addressed post leaves a
        # dangling ask no one can answer (259 such posts, 0.8% reply
        # rate). WARNING, not DEBUG — the cohort strip was logged at
        # DEBUG and 200 of them produced no operator-visible signal.
        rejection = self._post_type_rejection(
            agent,
            post_type,
            action_data.get("tagged_agent"),
            available_types,
        )
        if rejection is not None:
            logger.warning(
                "[%s] Phase 5: rejected new post in #%s — %s",
                agent.agent_id, channel, rejection,
            )
            agent.state.consecutive_phase5_skips = previous_skips + 1
            return
        # Layer 1-3 judge the JSON declaration, but the mutilation this
        # whole gate exists to prevent is driven by the message BODY.
        # A broadcast type with tagged_agent=null sails through the
        # check above even when the body itself @-mentions an
        # unreachable lab in prose — and the strip above would then
        # publish the post with that mention silently deleted,
        # producing exactly the dangling-ask artifact (measured in
        # production: 42 of 259 posts named a lab in prose with no
        # tag). Reject instead of publishing a mutilated body.
        if body_mention_was_stripped:
            logger.warning(
                "[%s] Phase 5: rejected new post in #%s — the message "
                "body @-mentions an agent this cohort gate cannot "
                "reach; publishing it would silently delete that "
                "mention rather than deliver it (post_type=%r)",
                agent.agent_id, channel, post_type,
            )
            agent.state.consecutive_phase5_skips = previous_skips + 1
            return
        # New top-level post
        posted = await self._post_message(agent.agent_id, channel, message_text, landed_check=True)
        if not posted:
            # _post_message already logged why (e.g. the text stripped to
            # empty). Nothing reached Slack, so neither the turn counter
            # nor an assessment row may be written for it — either would
            # be a phantom record with no corresponding Slack message.
            logger.info(
                "[%s] Phase 5: New post in #%s suppressed — not counted, "
                "nothing persisted",
                agent.agent_id, channel,
            )
        else:
            agent.message_count += 1
            self._proposals_posted += 1

            # No post type reaching here ever carries an assessment
            # sidecar anymore — the hub is hard-gated out of this
            # function entirely (see the docstring), and CANONICAL has
            # no entry for one (post_types.py). The extraction/persist
            # step that used to live here for `opportunity_assessment`
            # moved to `_reply_to_thread`'s Phase-4 CONCLUDE handling
            # (Option A relocation).

            # Check if it tags another agent
            tagged_agent = action_data.get("tagged_agent")
            if tagged_agent:
                logger.info(
                    "[%s] Phase 5: New post in #%s tagging @%s",
                    agent.agent_id, channel, tagged_agent,
                )
            else:
                logger.info(
                    "[%s] Phase 5: New post in #%s",
                    agent.agent_id, channel,
                )

    async def _rehydrate_proposal_count(self) -> None:
        """Recover this run's pitch count from the durable store on resume.

        A pitch is a BOT ``agent_messages`` row with ``phase == 'new_post'``
        for this run — the only top-level post kind (the hub is gated out of
        Phase 5, ``pi_lab`` is the only other role, and the two other
        _post_message callers write 'thread_reply' or the panel-note phase).
        The ``is_bot``/``agent_id`` filters keep parity with the live counter,
        which only increments in ``_phase5_new_post``: they exclude the rare
        mirrored human top-level post (``agent_id`` NULL) the Slack poller can
        record in a seeded channel, and collab_private posts are excluded the
        same way ``_count_today_posts`` excludes them (they are flat
        refinement replies/handovers, not spam-prevention-relevant pitches).

        This restores the COUNT only. Whether that count can still gate
        anything depends on ``self.max_proposals`` being the same nonzero
        target the run opened under — main.py's ``_run_simulation`` is what
        keeps that true across a resume, by inheriting ``max_proposals`` from
        the run's stored config when the CLI value is 0. If ``max_proposals``
        is 0 here, the query is skipped entirely: there is nothing to gate.
        """
        if not self.session_factory or self.simulation_run_id is None:
            return
        if self.max_proposals <= 0:
            return
        from sqlalchemy import func, select

        from src.models import AgentMessage
        async with self.session_factory() as db:
            count = (
                await db.execute(
                    select(func.count(AgentMessage.id)).where(
                        AgentMessage.simulation_run_id == self.simulation_run_id,
                        AgentMessage.phase == "new_post",
                        AgentMessage.is_bot.is_(True),
                        AgentMessage.agent_id.isnot(None),
                        AgentMessage.visibility != VISIBILITY_COLLAB_PRIVATE,
                    )
                )
            ).scalar_one()
        self._proposals_posted = int(count or 0)
        logger.info("Rehydrated proposal count: %d pitch(es) this run", self._proposals_posted)

    def _open_interview_count(self) -> int:
        """Distinct interview threads still open across all agents. The hub is
        in every interview thread; a distinct-set is robust even if it is not."""
        open_ids: set[str] = set()
        for agent in self.agents.values():
            for tid, t in agent.state.active_threads.items():
                if t.status == "active":
                    open_ids.add(tid)
        return len(open_ids)

    def _proposal_target_drained(self) -> bool:
        """True once the pitch cap is reached and no interview has been open
        for PROPOSAL_DRAIN_SETTLE_TICKS consecutive checks. A METHOD, not a
        property, because it MUTATES ``_proposal_drain_streak`` — call it
        exactly once per loop iteration (the main-loop condition does)."""
        if self.max_proposals <= 0 or self._proposals_posted < self.max_proposals:
            self._proposal_drain_streak = 0
            return False
        if self._open_interview_count() > 0:
            self._proposal_drain_streak = 0
            return False
        self._proposal_drain_streak += 1
        return self._proposal_drain_streak >= PROPOSAL_DRAIN_SETTLE_TICKS

    def _roles_by_agent(self) -> dict[str, str]:
        """Live roster agent_id -> role. Agents absent from this map (e.g.
        ``grantbot``, which has cohort memberships but no AgentRegistry row and
        is a separate process, not an entry in self.agents) match no post type's
        ``targets``."""
        return {aid: a.role for aid, a in self.agents.items()}

    def _post_types_for_role(self, role: str) -> tuple[PostTypeSpec, ...]:
        """``load_role(role).post_types``, cached.

        load_role() reads TOML from disk on every call — the same reason
        _role_rate_cache exists (see _calls_per_load). This runs once per
        phase-5 turn per agent; the cache keeps it off the disk.

        NOT the same trade-off as the tool allow-list: ``./prompts`` is
        bind-mounted, and ``tools_for_role`` (src/agent/tools.py) re-reads
        ``role.toml`` fresh on every call, so a live edit to a role's ``tools``
        key takes effect immediately. This cache means the same edit to a
        role's ``post_types`` key does NOT — it is picked up only on the next
        process restart. That asymmetry is a known trade-off, not a bug: this
        runs once per phase-5 turn per agent, which is the hot path the cache
        exists for, and there is currently no invalidation hook for it.
        """
        cached = self._role_post_types_cache.get(role)
        if cached is None:
            cached = deps.load_role(role).post_types
            self._role_post_types_cache[role] = cached
        return cached

    def _available_post_types(self, agent: Agent) -> tuple[PostTypeSpec, ...]:
        """Layer 1 ∩ layer 2: what this agent may post as a NEW top-level post.

        The SAME tuple is rendered into the prompt and used to judge the
        response, so the menu and the gate cannot disagree.

        Used to also take a ``restricted`` flag (the caller's
        ``blocked_for_regular``), forwarded to ``available_for`` as
        ``terminal_only`` so a blocked agent could still be offered a
        "reports finished work" type past the regular-work backpressure. That
        mechanism is gone along with the one post type it ever exempted (the
        hub's :mag: Opportunity Assessment — see post_types.py); a blocked
        caller now skips Phase 5 outright instead of calling in here at all
        (see ``_phase5_new_post``), so this always computes the unrestricted
        set.
        """
        return available_for(
            self._post_types_for_role(agent.role),
            gate=agent.allowed_sender_ids,
            roles_by_agent=self._roles_by_agent(),
            self_id=agent.agent_id,
        )

    def _normalize_tagged_agent(self, tagged_agent: object) -> object:
        """Recover a ``tagged_agent`` that names a real agent by a near-miss
        spelling, before the membership tests in ``_post_type_rejection`` run.

        The menu line a model reads offers both forms adjacent — `` `blackbird`
        (@BlackbirdBot) `` — so "@blackbird", "BlackbirdBot", "Blackbird", and
        " blackbird" are all one slip away from the exact agent_id the gate
        compares against, and an exact-string mismatch used to reject and
        publish nothing for every one of them.

        Conservative on purpose: resolves to an agent_id that demonstrably
        exists on the live roster (``self.agents``) or a bot name that
        demonstrably resolves via ``self._bot_name_to_id`` — never guesses at
        one that doesn't. Anything else (including non-string input) passes
        through unchanged, so an unresolved or genuinely unreachable name is
        still rejected downstream exactly as before.
        """
        if not isinstance(tagged_agent, str):
            return tagged_agent
        candidate = tagged_agent.strip()
        if candidate.startswith("@"):
            candidate = candidate[1:]
        if candidate in self.agents:
            return candidate
        lowered = candidate.lower()
        if lowered in self.agents:
            return lowered
        resolved = self._bot_name_to_id.get(lowered)
        if resolved is not None:
            return resolved
        return tagged_agent  # unresolved — pass through; still rejected below

    def _post_type_rejection(
        self,
        agent: Agent,
        post_type: str,
        tagged_agent: str | None,
        available: tuple[PostTypeSpec, ...],
    ) -> str | None:
        """Why this new top-level post must not be published, or None.

        Applies only to ``action: "new_post"`` — a reply is never gated here.

        Every rejection increments ``self._post_type_rejections[agent.agent_id]``
        (mirroring ``self._cohort_tags_stripped``), so a deployment where a
        role or model is having every post rejected is visible without
        grepping logs. Rejection messages always quote ``tagged_agent`` exactly
        as the model sent it — normalisation is for the membership tests only.
        """
        by_name = {s.name: s for s in available}

        def _reject(reason: str) -> str:
            self._post_type_rejections[agent.agent_id] = (
                self._post_type_rejections.get(agent.agent_id, 0) + 1
            )
            return reason

        spec = by_name.get(post_type)
        if spec is None:
            return _reject(
                f"post_type {post_type!r} is not available to role "
                f"{agent.role!r} with this topology "
                f"(available: {sorted(by_name) or 'none'})"
            )
        # Layers 2 and 3 are inert when the gate is off, so a mesh deployment's
        # behaviour is byte-identical after this change. Today a hallucinated
        # tagged_agent there is logged and the post ships; tightening that is a
        # separate decision, not a side effect of this one.
        if agent.allowed_sender_ids is None:
            return None
        # Normalise a near-miss spelling ("@blackbird", "BlackbirdBot", " blackbird")
        # onto the agent_id the membership tests below compare against. See
        # _normalize_tagged_agent's docstring — this never invents a target that
        # doesn't already resolve to a real agent.
        normalized_tag = self._normalize_tagged_agent(tagged_agent)
        allowed = eligible_targets(
            spec,
            gate=agent.allowed_sender_ids,
            roles_by_agent=self._roles_by_agent(),
            self_id=agent.agent_id,
        )
        if not spec.targets:
            # A broadcast type addresses no one, so the tag is redundant — but
            # redundant is not wrong. The hub used to post its :mag: assessment
            # into the PI's own channel, where naming that PI was the natural
            # thing to do; rejecting it would destroy the artifact and the whole
            # interview behind it over a field nothing routes on. Ignore a
            # REACHABLE tag; an unreachable one is still the dangling-ask bug.
            if normalized_tag and normalized_tag not in agent.allowed_sender_ids:
                return _reject(
                    f"post_type {post_type!r} addresses no one and "
                    f"tagged_agent={tagged_agent!r} is not reachable"
                )
            return None
        if not normalized_tag:
            return _reject(
                f"post_type {post_type!r} must address one of "
                f"{sorted(allowed)}, but tagged_agent was null"
            )
        if normalized_tag not in allowed:
            return _reject(
                f"tagged_agent={tagged_agent!r} is not reachable for post_type "
                f"{post_type!r} (allowed: {sorted(allowed)})"
            )
        return None

    def _parse_phase5_response(self, response: str) -> tuple[dict | None, str | None]:
        """Parse Phase 5 response into (json_data, message_text).

        Expects JSON block + <slack_message> tags.  Uses the LAST JSON code
        block so that if the LLM revises its decision mid-response the final
        action wins.  Requires <slack_message> tags for the message body —
        raw text after the JSON block is never used (prevents reasoning leakage).

        The <assessment_json> sidecar is stripped from the source ONCE, up
        front, before either the fenced-block search or the raw-JSON fallback
        runs — not just the fallback. The sidecar is meant to be bare JSON
        with no fence (see _extract_assessment_json's docstring), but a model
        routinely wraps it in a ```json``` fence anyway despite the prompt's
        instructions. Since the sidecar is emitted LAST (after the action
        fence and the <slack_message> block), a fenced sidecar becomes the
        LAST ```json``` block in the raw response — exactly the one this
        method is looking for — and would silently hijack the action parse:
        the verdict dict gets treated as the action, `action` and `channel`
        fall back to defaults, and the real action is lost. Stripping first
        removes the sidecar (fence and all, since the tags wrap the fence)
        before the action search ever runs, so a fenced sidecar can no longer
        reach it.
        """
        data = None
        stripped = _strip_assessment_sidecar(response)
        try:
            # Find the LAST ```json``` block (LLM may revise mid-response).
            json_matches = list(
                re.finditer(r"```json\s*\n(.*?)\n```", stripped, re.DOTALL)
            )
            if json_matches:
                data = json.loads(json_matches[-1].group(1))
            else:
                # Try finding raw JSON in the same sidecar-stripped source —
                # without the strip, a sidecar-only response (no action fence
                # present at all) would be indistinguishable from the action
                # and get parsed as one, silently discarding a legitimate
                # Phase 5 turn.
                json_start = stripped.find("{")
                json_end = (
                    stripped.find("}", json_start) + 1
                    if json_start >= 0 else -1
                )
                if json_start >= 0 and json_end > json_start:
                    data = json.loads(stripped[json_start:json_end])
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Failed to parse Phase 5 JSON: %s", exc)

        if not data:
            return None, None

        # Extract message from <slack_message> tags (required — no raw-text fallback).
        # Anchor on the LAST tag pair so a prior mention of the tag name in the
        # LLM's reasoning (e.g. "my output is a single `<slack_message>` block")
        # does not pull reasoning into the captured body.
        last_close = response.rfind("</slack_message>")
        if last_close >= 0:
            last_open = response.rfind("<slack_message>", 0, last_close)
            if last_open >= 0:
                body = response[last_open + len("<slack_message>"):last_close].strip()
                return data, body

        return data, None
