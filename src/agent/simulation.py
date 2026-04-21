"""Turn-based simulation engine — coordinates all agents across all channels."""

import asyncio
import json
import logging
import random
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.agent.agent import Agent
from src.agent.channels import SEEDED_CHANNELS
from src.agent.foa_cache import extract_foa_number, format_foa_for_prompt
from src.agent.funding_rules import (
    format_funding_thread_summary,
    format_your_prior_messages,
    is_acknowledgment_only_funding_reply,
    is_announcement_only_funding_reply,
    summarize_funding_thread,
)
from src.agent.message_log import LogEntry, MessageLog, is_funding_post
from src.agent.state import PostRef, ProposalRef, ThreadState
from src.agent.tools import TOOL_DEFINITIONS, execute_tool
from src.config import get_settings
from src.models import AgentMessage, LlmCallLog, ProposalReview, SimulationRun, ThreadDecision
from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE, VISIBILITY_PUBLIC
from src.services.llm import (
    generate_agent_response,
    generate_with_tools,
    set_call_log_callback,
)

logger = logging.getLogger(__name__)


def _visibility_permits(origin: str, current: str) -> bool:
    """True iff an origin-visibility record may appear in a current-visibility context.

    Implements the ordering `public < collab_private` from G3:
    - public origins are visible in any context.
    - collab_private origins are visible only in a collab_private context.

    See specs/privacy-and-channel-visibility.md §G3.
    """
    if origin == VISIBILITY_PUBLIC:
        return True
    return current == VISIBILITY_COLLAB_PRIVATE

# Pilot lab configurations
PILOT_LABS = [
    {"id": "su", "name": "SuBot", "pi": "Andrew Su"},
    {"id": "wiseman", "name": "WisemanBot", "pi": "Luke Wiseman"},
    {"id": "lotz", "name": "LotzBot", "pi": "Martin Lotz"},
    {"id": "cravatt", "name": "CravattBot", "pi": "Benjamin Cravatt"},
    {"id": "grotjahn", "name": "GrotjahnBot", "pi": "Danielle Grotjahn"},
    {"id": "petrascheck", "name": "PetrascheckBot", "pi": "Michael Petrascheck"},
    {"id": "ken", "name": "KenBot", "pi": "Megan Ken"},
    {"id": "racki", "name": "RackiBot", "pi": "Lisa Racki"},
    {"id": "saez", "name": "SaezBot", "pi": "Enrique Saez"},
    {"id": "wu", "name": "WuBot", "pi": "Chunlei Wu"},
    {"id": "ward", "name": "WardBot", "pi": "Andrew Ward"},
    {"id": "briney", "name": "BrineyBot", "pi": "Bryan Briney"},
    {"id": "forli", "name": "ForliBot", "pi": "Stefano Forli"},
    {"id": "deniz", "name": "DenizBot", "pi": "Ashok Deniz"},
    {"id": "lairson", "name": "LairsonBot", "pi": "Luke Lairson"},
]

# Keywords for channel-profile matching
_CHANNEL_KEYWORDS: dict[str, list[str]] = {
    "drug-repurposing": [
        "drug", "repurpos", "pharmacolog", "therapeutic", "compound",
        "small molecule", "target", "ligand", "polypharmacol",
    ],
    "structural-biology": [
        "structur", "cryo", "crystallograph", "x-ray", "microscop",
        "tomograph", "molecular visualization", "conformation",
    ],
    "aging-and-longevity": [
        "aging", "longevity", "lifespan", "neurodegenerat", "age-related",
        "senescen", "alzheimer", "parkinson",
    ],
    "single-cell-omics": [
        "single-cell", "single cell", "scrna", "transcriptom", "genomic",
        "multiom", "sequencing", "omics",
    ],
    "chemical-biology": [
        "chemical biolog", "proteomics", "chemoproteom", "covalent",
        "activity-based", "abpp", "chemical probe", "mass spectrom",
    ],
}
_UNIVERSAL_CHANNELS = {"general", "funding-opportunities"}

# Slack poll throttles. PI messages come from humans, so sub-turn latency is
# unnecessary; polling every turn was saturating one bot token's rate limit.
CHANNEL_POLL_INTERVAL = 15.0   # seconds between conversations.history sweeps
PROPOSAL_POLL_INTERVAL = 30.0  # seconds between conversations.replies sweeps


class SimulationEngine:
    """
    Turn-based simulation engine.

    Main loop: poll Slack for PI messages, select agent, run 5-phase turn.
    """

    def __init__(
        self,
        agents: list[Agent],
        slack_clients: dict,  # agent_id -> AgentSlackClient
        max_runtime_minutes: int = 60,
        budget_cap: int = 50,
        session_factory=None,
        simulation_run_id: uuid.UUID | None = None,
        reset_cursors: bool = False,
    ):
        self.agents = {a.agent_id: a for a in agents}
        self.slack_clients = slack_clients
        self.max_runtime_minutes = max_runtime_minutes
        self.budget_cap = budget_cap
        self.session_factory = session_factory
        self.simulation_run_id = simulation_run_id
        self._reset_cursors = reset_cursors

        self._start_time: datetime | None = None
        self._running = False
        self.message_log = MessageLog()
        self._pi_slack_id_to_agent_ids: dict[str, list[str]] = {}  # PI slack_user_id -> [agent_ids]
        self._dm_poll_cursors: dict[str, str] = {}  # agent_id -> latest DM ts
        self._pi_handler = None  # Initialized in start() after PI mappings loaded

        # Agent name lookups
        self._bot_name_to_id: dict[str, str] = {
            a.bot_name.lower(): a.agent_id for a in agents
        }
        self.message_log.set_bot_name_map(self._bot_name_to_id)

        # LLM call log buffer
        self._llm_log_buffer: list[dict] = []
        self._llm_log_flush_size = 10

        # Channel ID map (populated during setup)
        self._channel_id_map: dict[str, str] = {}  # name -> id
        # Channel visibility map (populated from agent_channels.visibility
        # during setup; defaults to 'public' for any name not present). Used
        # by G1 prompt scoping and G3 dedup filtering.
        self._channel_visibility: dict[str, str] = {}  # name -> 'public' | 'collab_private'
        # Per-private-channel member-bot set (channel_id -> {agent_id, ...}).
        # Used to route polling/history calls through a bot that can actually
        # see the private channel; non-member bots get channel_not_found from
        # Slack for private channels they aren't in.
        self._private_channel_members: dict[str, set[str]] = {}

        # Slack poll cursor: channel_id -> latest ts seen
        self._poll_cursors: dict[str, str] = {}

        # Closed thread IDs — prevents Phase 3 from re-activating decided threads
        self._closed_thread_ids: set[str] = set()

        # Prior thread decisions per agent pair — for Phase 5 dedup context.
        # Key: tuple(sorted([agent_a, agent_b])), Value: list of dicts
        self._prior_threads: dict[tuple[str, str], list[dict]] = {}

        # Thread IDs already reopened via DB-synced PI guidance (rating=0 reviews)
        # to avoid re-processing on every turn.
        self._db_reopened_thread_ids: set[str] = set()

        # Last agent to make an LLM call — prevents the same agent from making
        # back-to-back LLM calls when it's the only active agent.
        self._last_llm_caller: str | None = None

        # Wall-clock throttles for Slack pollers + round-robin cursor over
        # connected clients, so one agent's token doesn't carry all poll load.
        self._last_channel_poll: float = 0.0
        self._last_proposal_poll: float = 0.0
        self._poll_client_cursor: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_within_time_limit(self) -> bool:
        if self.max_runtime_minutes <= 0:
            return True  # run forever (until SIGTERM)
        if not self._start_time:
            return True
        elapsed = (datetime.now(timezone.utc) - self._start_time).total_seconds()
        return elapsed < self.max_runtime_minutes * 60

    def _agent_within_budget(self, agent: Agent) -> bool:
        if self.budget_cap <= 0:
            return True  # unlimited
        return agent.api_call_count < self.budget_cap

    def _non_funding_thread_count(self, agent: Agent) -> int:
        """Count active threads that are NOT funding-related."""
        return sum(
            1 for t in agent.state.active_threads.values()
            if not self.message_log.is_funding_thread(t.thread_id)
        )

    def _count_today_posts(self, agent: Agent) -> int:
        """Count top-level posts by this agent in the current Pacific time day."""
        from zoneinfo import ZoneInfo
        pacific = ZoneInfo("America/Los_Angeles")
        today_start = datetime.now(pacific).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).timestamp()
        return sum(
            1 for e in self.message_log.get_agent_top_level_posts(agent.agent_id, limit=100)
            if e.posted_at >= today_start
        )

    async def start(self) -> None:
        """Run the full simulation."""
        self._start_time = datetime.now(timezone.utc)
        self._running = True
        settings = get_settings()

        logger.info(
            "Simulation started. Max runtime: %dm, Budget: %d calls/agent",
            self.max_runtime_minutes, self.budget_cap,
        )

        # Setup
        self._ensure_seeded_channels()
        # Load any collab_private channels created via the web-UI reopen flow
        # BEFORE rebuilding state so the rebuild's history-fetch loop covers
        # them too — otherwise the handover message wouldn't land in the
        # message log until the first per-turn poll tick.
        await self._sync_private_channels_from_db()
        self._build_lab_directories()
        await self._load_pi_mappings()
        await self._rebuild_state_from_slack()
        # Rebuild advanced last_seen_cursor to max(all_messages), which can
        # overshoot messages in private channels (typically older than the
        # latest public chatter). Rewind member-bot cursors so Phase 2 can
        # still scan the handover and any subsequent private-channel activity.
        self._rewind_cursors_for_private_channels()
        set_call_log_callback(self._on_llm_call)

        # Backfill FOA cache for any previously posted opportunities
        await self._backfill_foa_cache()

        # Initialize PI handler after mappings are loaded
        from src.agent.pi_handler import PIHandler
        self._pi_handler = PIHandler(
            agents=self.agents,
            slack_clients=self.slack_clients,
            pi_slack_id_to_agent_ids=self._pi_slack_id_to_agent_ids,
            message_log=self.message_log,
            session_factory=self.session_factory,
        )

        # Main loop
        turn_count = 0
        consecutive_idle = 0
        while self._running and self.is_within_time_limit:
            # Poll Slack for PI messages (channels, DMs, and proposal threads)
            await self._poll_slack_for_pi_messages()
            await self._poll_pi_dms()
            await self._poll_proposal_threads_for_pi()

            # Sync proposal reviews and any newly-created private channels from
            # the web app. Both are DB-driven, so a single tick picks them up.
            await self._sync_proposal_reviews_from_db()
            await self._sync_private_channels_from_db()

            # Select agent
            agent = self._select_agent()
            if not agent or not self._agent_within_budget(agent):
                # All agents over budget
                logger.info("All agents over budget or no agent selected. Stopping.")
                break

            # Prevent the same agent from making back-to-back LLM calls.
            # If this agent was the last to make an LLM call, skip its turn
            # so other agents get a chance (or the simulation idles).
            if self._last_llm_caller == agent.agent_id:
                agent.state.last_selected = time.time()
                consecutive_idle += 1
                if consecutive_idle <= 3:
                    delay = 5
                elif consecutive_idle <= 10:
                    delay = 15
                else:
                    delay = 30
                logger.debug(
                    "[%s] Skipped: was last LLM caller (idle backoff: %ds)",
                    agent.agent_id, delay,
                )
                await asyncio.sleep(delay)
                continue

            logger.info("=== Turn %d: %s ===", turn_count + 1, agent.agent_id)

            # Run 5-phase turn
            did_work = False
            try:
                did_work = await self._run_turn(agent)
            except Exception:
                logger.exception("Error during turn for %s", agent.agent_id)

            # Track last agent to make an LLM call
            if did_work:
                self._last_llm_caller = agent.agent_id

            # Update last_selected
            agent.state.last_selected = time.time()
            turn_count += 1

            # Idle backoff: if no LLM calls were made, delay before next turn
            if did_work:
                consecutive_idle = 0
            else:
                consecutive_idle += 1

            if consecutive_idle > 0:
                if consecutive_idle <= 3:
                    delay = 5
                elif consecutive_idle <= 10:
                    delay = 15
                else:
                    delay = 30
                logger.debug("Idle backoff: %ds (idle streak: %d)", delay, consecutive_idle)
                await asyncio.sleep(delay)
            elif settings.turn_delay_seconds > 0:
                await asyncio.sleep(settings.turn_delay_seconds)

            # Flush LLM logs periodically
            if self._llm_log_buffer:
                await self._flush_llm_logs()

        logger.info("Main loop exited after %d turns", turn_count)

    async def stop(self) -> None:
        """Stop the simulation gracefully."""
        self._running = False
        set_call_log_callback(None)
        await self._flush_llm_logs()
        logger.info("Simulation stopping...")

    # ------------------------------------------------------------------
    # Agent selection (weighted random)
    # ------------------------------------------------------------------

    def _select_agent(self) -> Agent | None:
        """Weighted random selection: P(agent) ∝ (now - agent.last_selected).

        Agents with consecutive Phase 5 skips get a weight penalty:
        weight is divided by 2^(skips - 2) once skips >= 3.
        """
        now = time.time()
        candidates = [
            a for a in self.agents.values()
            if self._agent_within_budget(a)
        ]
        if not candidates:
            return None

        weights = []
        for a in candidates:
            w = max(now - a.state.last_selected, 1.0)
            skips = a.state.consecutive_phase5_skips
            if skips >= 3:
                w /= 2 ** (skips - 2)
            weights.append(w)
        return random.choices(candidates, weights=weights, k=1)[0]

    # ------------------------------------------------------------------
    # Turn execution (5 phases)
    # ------------------------------------------------------------------

    async def _run_turn(self, agent: Agent) -> bool:
        """Run all 5 phases for a single agent turn. Returns True if work was done."""
        settings = get_settings()
        api_calls_before = agent.api_call_count

        # Phase 1: Channel discovery
        self._phase1_channel_discovery(agent)

        # Phase 2: Scan & filter new posts
        await self._phase2_scan_filter(agent)

        # Phase 3: Activate threads from tags and replies
        self._phase3_activate_threads(agent)

        # Phase 4: Reply to active threads (parallel)
        phase4_thread_ids = await self._phase4_reply_threads(agent)

        # Phase 4 activity resets skip backoff — agent is actively engaged
        if phase4_thread_ids:
            agent.state.consecutive_phase5_skips = 0
            agent.state.last_phase5_action_time = time.time()

        # State-change gate: skip Phase 5 (no LLM call) unless there's
        # new actionable state or the spontaneous post timer has expired.
        phase2_ran = agent.api_call_count > api_calls_before
        has_interesting = len(agent.state.interesting_posts) > 0
        has_phase4_work = len(phase4_thread_ids) > 0
        has_pi = agent.state.has_pi_directive

        # Spontaneous post timer — allow one Phase 5 call after enough
        # idle time so agents can organically start new conversations.
        base_interval = settings.phase5_spontaneous_interval * 60  # to seconds
        skips = agent.state.consecutive_phase5_skips
        stretch = min(max(skips, 1), settings.phase5_spontaneous_interval_max_multiplier)
        spontaneous_interval = base_interval * stretch
        since_last_action = time.time() - agent.state.last_phase5_action_time
        spontaneous_ready = since_last_action >= spontaneous_interval

        has_new_work = has_interesting or has_phase4_work or phase2_ran or has_pi

        if has_new_work or spontaneous_ready:
            await self._phase5_new_post(agent, phase4_thread_ids)
        else:
            logger.debug(
                "[%s] Phase 5: Skipped (no state change, spontaneous in %ds)",
                agent.agent_id,
                int(spontaneous_interval - since_last_action),
            )

        # Clear PI directive flag after the turn
        agent.state.has_pi_directive = False

        # Update cursor
        agent.state.last_seen_cursor = time.time()

        return agent.api_call_count > api_calls_before

    # ------------------------------------------------------------------
    # Phase 1: Channel Discovery
    # ------------------------------------------------------------------

    def _phase1_channel_discovery(self, agent: Agent) -> None:
        """Join new channels based on profile keyword matching."""
        profile_text = agent.public_profile.lower()
        channels_to_join = set(_UNIVERSAL_CHANNELS)

        for channel_name, keywords in _CHANNEL_KEYWORDS.items():
            if any(kw in profile_text for kw in keywords):
                channels_to_join.add(channel_name)

        new_channels = channels_to_join - agent.state.subscribed_channels
        if new_channels:
            for ch_name in new_channels:
                ch_id = self._channel_id_map.get(ch_name)
                if ch_id:
                    client = self.slack_clients.get(agent.agent_id)
                    if client:
                        client.join_channel(ch_id)
            agent.state.subscribed_channels.update(new_channels)
            logger.info("[%s] Phase 1: Joined channels: %s", agent.agent_id, new_channels)

    # ------------------------------------------------------------------
    # Phase 2: Scan & Filter
    # ------------------------------------------------------------------

    async def _phase2_scan_filter(self, agent: Agent) -> None:
        """Scan new top-level posts and decide which to add to interesting_posts."""
        settings = get_settings()

        # Get new top-level posts since agent's last turn
        new_posts = self.message_log.get_new_top_level_posts(
            since=agent.state.last_seen_cursor,
            channels=agent.state.subscribed_channels,
            exclude_agent_id=agent.agent_id,
        )

        # Exclude posts already in interesting_posts or active_threads
        known_ids = {p.post_id for p in agent.state.interesting_posts}
        known_ids.update(agent.state.active_threads.keys())
        new_posts = [p for p in new_posts if p.ts not in known_ids]

        if not new_posts:
            logger.debug("[%s] Phase 2: No new posts to evaluate", agent.agent_id)
            return

        # Build post data for LLM
        post_dicts = [
            {
                "post_id": p.ts,
                "channel": p.channel,
                "sender": p.sender_name,
                "content_snippet": p.content,
            }
            for p in new_posts
        ]

        system_prompt, messages = agent.build_phase2_scan_prompt(post_dicts)

        agent.api_call_count += 1
        try:
            response = await generate_agent_response(
                system_prompt=system_prompt,
                messages=messages,
                max_tokens=500,
                log_meta={"agent_id": agent.agent_id, "phase": "scan"},
            )
            if not response or not response.strip():
                logger.warning("[%s] Phase 2: Empty response from LLM, skipping", agent.agent_id)
                return
            result = _extract_json(response)
            selected_ids = set(result.get("selected_post_ids", []))

            # Add selected posts to interesting_posts
            for post in new_posts:
                if post.ts in selected_ids:
                    foa_num = None
                    snippet_len = 200
                    if is_funding_post(post.content):
                        foa_num = extract_foa_number(post.content)
                        snippet_len = 500  # funding posts need more context
                    agent.state.interesting_posts.append(PostRef(
                        post_id=post.ts,
                        channel=post.channel,
                        sender_agent_id=post.sender_agent_id or post.sender_name,
                        content_snippet=post.content[:snippet_len],
                        posted_at=post.posted_at,
                        foa_number=foa_num,
                    ))

            logger.info(
                "[%s] Phase 2: Evaluated %d posts, added %d to interesting",
                agent.agent_id, len(new_posts), len(selected_ids),
            )
        except Exception as exc:
            logger.error("[%s] Phase 2 scan failed: %s", agent.agent_id, exc)

        # Prune if over cap
        if len(agent.state.interesting_posts) > settings.interesting_posts_cap:
            await self._phase2_prune(agent)

    async def _phase2_prune(self, agent: Agent) -> None:
        """Prune interesting_posts to ≤ cap."""
        system_prompt, messages = agent.build_phase2_prune_prompt()

        agent.api_call_count += 1
        try:
            response = await generate_agent_response(
                system_prompt=system_prompt,
                messages=messages,
                max_tokens=500,
                log_meta={"agent_id": agent.agent_id, "phase": "prune"},
            )
            if not response or not response.strip():
                logger.warning("[%s] Phase 2 prune: empty response", agent.agent_id)
                return
            result = _extract_json(response)
            keep_ids = set(result.get("keep_post_ids", []))

            before = len(agent.state.interesting_posts)
            agent.state.interesting_posts = [
                p for p in agent.state.interesting_posts if p.post_id in keep_ids
            ]
            logger.info(
                "[%s] Phase 2 prune: %d → %d",
                agent.agent_id, before, len(agent.state.interesting_posts),
            )
        except Exception as exc:
            logger.error("[%s] Phase 2 prune failed: %s", agent.agent_id, exc)

    # ------------------------------------------------------------------
    # Phase 3: Activate Threads from Tags
    # ------------------------------------------------------------------

    def _phase3_activate_threads(self, agent: Agent) -> None:
        """
        Auto-activate threads where this agent was tagged or
        where someone replied to this agent's top-level posts.
        """
        settings = get_settings()
        cursor = agent.state.last_seen_cursor

        # Check for tags
        tagged_entries = self.message_log.get_tags_for_agent(agent.bot_name, cursor)
        for entry in tagged_entries:
            thread_id = entry.thread_ts or entry.ts
            if thread_id in agent.state.active_threads:
                continue
            if thread_id in self._closed_thread_ids:
                continue
            is_funding = self.message_log.is_funding_thread(thread_id)
            if not is_funding and self._non_funding_thread_count(agent) >= settings.active_thread_threshold:
                break
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
                # Extract FOA number from root post for funding threads
                foa_num = None
                if is_funding:
                    root = self.message_log.get_entry(thread_id)
                    if root:
                        foa_num = extract_foa_number(root.content)
                agent.state.active_threads[thread_id] = ThreadState(
                    thread_id=thread_id,
                    channel=entry.channel,
                    other_agent_id=other_id,
                    message_count=self.message_log.get_thread_message_count(thread_id),
                    has_pending_reply=True,
                    foa_number=foa_num,
                )
                logger.info(
                    "[%s] Phase 3: Activated thread %s (tagged by %s)",
                    agent.agent_id, thread_id, other_id,
                )

        # Check for replies to agent's own top-level posts
        reply_entries = self.message_log.get_replies_to_agent_posts(agent.agent_id, cursor)
        for entry in reply_entries:
            thread_id = entry.thread_ts
            if not thread_id or thread_id in agent.state.active_threads:
                continue
            if thread_id in self._closed_thread_ids:
                continue
            is_funding = self.message_log.is_funding_thread(thread_id)
            if not is_funding and self._non_funding_thread_count(agent) >= settings.active_thread_threshold:
                break
            # Check thread participation rules
            allowed = self.message_log.get_thread_allowed_agents(thread_id)
            if allowed and len(allowed) >= 2 and agent.agent_id not in allowed:
                continue
            other_id = self._infer_agent_id(entry.sender_name) or entry.sender_agent_id
            if other_id and other_id != agent.agent_id:
                # Extract FOA number from root post for funding threads
                foa_num = None
                if is_funding:
                    root = self.message_log.get_entry(thread_id)
                    if root:
                        foa_num = extract_foa_number(root.content)
                agent.state.active_threads[thread_id] = ThreadState(
                    thread_id=thread_id,
                    channel=entry.channel,
                    other_agent_id=other_id,
                    message_count=self.message_log.get_thread_message_count(thread_id),
                    has_pending_reply=True,
                    foa_number=foa_num,
                )
                logger.info(
                    "[%s] Phase 3: Activated thread %s (reply from %s)",
                    agent.agent_id, thread_id, other_id,
                )

    # ------------------------------------------------------------------
    # Phase 4: Reply to Active Threads (parallel)
    # ------------------------------------------------------------------

    async def _phase4_reply_threads(self, agent: Agent) -> set[str]:
        """Reply to all active threads that have a pending reply from the other agent.

        Returns the set of thread IDs that were replied to (so Phase 5 can skip them).
        """
        settings = get_settings()

        # Identify threads needing a reply
        threads_to_reply: list[ThreadState] = []
        for thread in agent.state.active_threads.values():
            if thread.status != "active":
                continue
            # Check if there's a new reply from the other agent
            has_new = self.message_log.has_new_reply_from_other(
                thread.thread_id, agent.agent_id, agent.state.last_seen_cursor,
            )
            if has_new or thread.has_pending_reply:
                threads_to_reply.append(thread)

        if not threads_to_reply:
            logger.debug("[%s] Phase 4: No threads needing reply", agent.agent_id)
            return set()

        logger.info(
            "[%s] Phase 4: Replying to %d threads",
            agent.agent_id, len(threads_to_reply),
        )

        # Run replies in parallel
        tasks = [
            self._reply_to_thread(agent, thread)
            for thread in threads_to_reply
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

        return {t.thread_id for t in threads_to_reply}

    async def _reply_to_thread(self, agent: Agent, thread: ThreadState) -> None:
        """Compose and post a reply to a single thread."""
        settings = get_settings()

        # Get thread history from message log
        history_entries = self.message_log.get_thread_history(thread.thread_id)
        thread_history = [
            {"sender": e.sender_name, "content": e.content}
            for e in history_entries
        ]

        # Update message count (subtract offset for PI-reopened threads)
        thread.message_count = len(history_entries) - thread.message_count_offset

        # Final participation check before composing a reply
        allowed = self.message_log.get_thread_allowed_agents(thread.thread_id)
        if allowed and agent.agent_id not in allowed:
            logger.info(
                "[%s] Phase 4: Aborting reply to thread %s — not in allowed set %s",
                agent.agent_id, thread.thread_id, allowed,
            )
            agent.state.active_threads.pop(thread.thread_id, None)
            return

        # Check for system-enforced close
        if thread.message_count >= settings.max_thread_messages:
            logger.info(
                "[%s] Thread %s reached max messages, closing",
                agent.agent_id, thread.thread_id,
            )
            await self._close_thread(agent, thread, "timeout")
            return

        # Get other agent info
        other_agent = self.agents.get(thread.other_agent_id)
        other_name = other_agent.bot_name if other_agent else thread.other_agent_id
        other_lab = other_agent.pi_name if other_agent else "Unknown"

        # Funding-thread context (self-dedup + late-joiner summary)
        is_funding = self.message_log.is_funding_thread(thread.thread_id)
        your_prior_text: str | None = None
        thread_activity_text: str | None = None
        if is_funding:
            your_prior_entries = [
                e for e in history_entries if e.sender_agent_id == agent.agent_id
            ]
            your_prior_text = format_your_prior_messages(your_prior_entries)
            summary = summarize_funding_thread(
                self.message_log, thread.thread_id, viewer_agent_id=agent.agent_id,
            )
            thread_activity_text = format_funding_thread_summary(summary)

        # Resolve the thread's channel visibility for G1 prompt scoping. In v1
        # all threads live in public channels, so this is effectively always
        # VISIBILITY_PUBLIC; the lookup hook is in place for when migrations
        # start producing collab_private channels.
        thread_visibility = self._resolve_channel_visibility(thread.channel)
        thread_channel_id = self._channel_id_map.get(thread.channel)

        # Build prompt
        system_prompt, messages = agent.build_phase4_prompt(
            thread=thread,
            thread_history=thread_history,
            other_agent_name=other_name,
            other_agent_lab=other_lab,
            is_funding_thread=is_funding,
            your_prior_messages=your_prior_text,
            thread_activity_summary=thread_activity_text,
            visibility=thread_visibility,
            channel_id=thread_channel_id,
        )

        # Create tool executor bound to this thread's state
        async def tool_executor(tool_name: str, tool_input: dict) -> str:
            return await execute_tool(tool_name, tool_input, agent.agent_id, thread)

        agent.api_call_count += 1
        try:
            response_text = await generate_with_tools(
                system_prompt=system_prompt,
                messages=messages,
                tools=TOOL_DEFINITIONS,
                tool_executor=tool_executor,
                model=settings.llm_agent_model_opus,
                max_tokens=1500,
                log_meta={
                    "agent_id": agent.agent_id,
                    "phase": "thread_reply",
                    "channel": thread.channel,
                },
            )

            # Extract message from <slack_message> tags, fall back to preamble stripping
            response_text = _extract_slack_message(response_text)

            if not response_text or not response_text.strip():
                logger.warning(
                    "[%s] Phase 4: Empty/unparseable response for thread %s, skipping",
                    agent.agent_id, thread.thread_id,
                )
                return

            # Funding-thread draft validators: reject announcement-only and
            # acknowledgment-only replies before they hit Slack.
            if is_funding:
                rejected_reason = None
                if is_announcement_only_funding_reply(response_text):
                    rejected_reason = "announcement-only"
                elif is_acknowledgment_only_funding_reply(response_text):
                    rejected_reason = "acknowledgment-only"
                if rejected_reason:
                    thread.funding_reject_count += 1
                    logger.info(
                        "[%s] Phase 4: Rejected %s draft in funding thread %s (count=%d)",
                        agent.agent_id, rejected_reason, thread.thread_id,
                        thread.funding_reject_count,
                    )
                    if thread.funding_reject_count >= 2:
                        # Back off: drop the pending-reply flag so the agent
                        # stops re-attempting this thread for a while.
                        thread.has_pending_reply = False
                        logger.info(
                            "[%s] Phase 4: Backing off funding thread %s after %d rejections",
                            agent.agent_id, thread.thread_id, thread.funding_reject_count,
                        )
                    return

            # Post the reply
            await self._post_message(
                agent.agent_id, thread.channel, response_text,
                thread_ts=thread.thread_id,
            )
            agent.message_count += 1
            thread.has_pending_reply = False
            thread.funding_reject_count = 0

            # Check for thread outcome
            await self._check_thread_outcome(agent, thread, response_text)

        except Exception as exc:
            logger.error(
                "[%s] Phase 4 reply to thread %s failed: %s",
                agent.agent_id, thread.thread_id, exc,
            )

    async def _check_thread_outcome(
        self,
        agent: Agent,
        thread: ThreadState,
        latest_reply: str,
    ) -> None:
        """Check if a thread should be closed based on the latest reply."""
        # Check for ✅ confirmation of a :memo: Summary
        if "✅" in latest_reply:
            # Look back in thread history for the latest :memo: Summary from the other agent
            history = self.message_log.get_thread_history(thread.thread_id)
            for entry in reversed(history):
                if entry.sender_agent_id == thread.other_agent_id and ":memo:" in entry.content:
                    # Proposal confirmed!
                    logger.info(
                        "[%s] Thread %s: proposal confirmed with ✅",
                        agent.agent_id, thread.thread_id,
                    )
                    # Extract text starting from :memo: marker
                    memo_idx = entry.content.find(":memo:")
                    summary_text = entry.content[memo_idx:].strip() if memo_idx >= 0 else entry.content
                    agent.state.pending_proposals = [
                        p for p in agent.state.pending_proposals
                        if p.thread_id != thread.thread_id
                    ]
                    agent.state.pending_proposals.append(ProposalRef(
                        thread_id=thread.thread_id,
                        channel=thread.channel,
                        other_agent_id=thread.other_agent_id,
                        summary_text=summary_text,
                        proposed_at=time.time(),
                    ))
                    await self._close_thread(agent, thread, "proposal", summary_text)
                    return

        # Check if this agent posted a :memo: Summary
        if ":memo:" in latest_reply:
            # The other agent needs to confirm — thread stays active
            thread.status = "active"
            logger.info(
                "[%s] Thread %s: posted :memo: Summary, waiting for ✅",
                agent.agent_id, thread.thread_id,
            )
            return

        # Check for ⏸️ — explicit "no viable collaboration" signal
        if "⏸️" in latest_reply or ":pause_button:" in latest_reply:
            logger.info(
                "[%s] Thread %s: ⏸️ no-proposal close",
                agent.agent_id, thread.thread_id,
            )
            await self._close_thread(agent, thread, "no_proposal")

    async def _close_thread(
        self,
        agent: Agent,
        thread: ThreadState,
        outcome: str,
        summary_text: str | None = None,
    ) -> None:
        """Close a thread and log the decision."""
        thread.status = "closed"
        self._closed_thread_ids.add(thread.thread_id)

        # Track for Phase 5 dedup context
        pair_key = tuple(sorted([agent.agent_id, thread.other_agent_id]))
        self._prior_threads.setdefault(pair_key, []).append({
            "channel": thread.channel,
            "outcome": outcome,
            "summary": (summary_text or "")[:400] or None,
        })
        # Remove from active threads
        agent.state.active_threads.pop(thread.thread_id, None)

        # Also close for the other agent if they have this thread active
        other_agent = self.agents.get(thread.other_agent_id)
        if other_agent and thread.thread_id in other_agent.state.active_threads:
            other_agent.state.active_threads[thread.thread_id].status = "closed"
            other_agent.state.active_threads.pop(thread.thread_id, None)
            # If proposal, add to other agent's pending_proposals too.
            # Replace any existing entry for the same thread so reopen/re-propose
            # cycles don't accumulate duplicates during a single run.
            if outcome == "proposal" and summary_text:
                other_agent.state.pending_proposals = [
                    p for p in other_agent.state.pending_proposals
                    if p.thread_id != thread.thread_id
                ]
                other_agent.state.pending_proposals.append(ProposalRef(
                    thread_id=thread.thread_id,
                    channel=thread.channel,
                    other_agent_id=agent.agent_id,
                    summary_text=summary_text,
                    proposed_at=time.time(),
                ))

        # Log to DB
        if self.session_factory and self.simulation_run_id:
            try:
                async with self.session_factory() as db:
                    decision = ThreadDecision(
                        simulation_run_id=self.simulation_run_id,
                        thread_id=thread.thread_id,
                        channel=thread.channel,
                        agent_a=agent.agent_id,
                        agent_b=thread.other_agent_id,
                        outcome=outcome,
                        summary_text=summary_text,
                    )
                    db.add(decision)
                    await db.commit()
            except Exception as exc:
                logger.warning("Failed to log thread decision: %s", exc)

        logger.info(
            "[%s] Thread %s closed: %s",
            agent.agent_id, thread.thread_id, outcome,
        )

        # Notify PI via DM
        if self._pi_handler:
            try:
                await self._pi_handler.notify_thread_conclusion(
                    agent.agent_id, thread, outcome, summary_text,
                )
            except Exception as exc:
                logger.debug("Failed to notify PI of thread conclusion: %s", exc)

        # Update working memory for both agents
        event = f"Thread in #{thread.channel} with {thread.other_agent_id} closed: {outcome}"
        if summary_text:
            event += f". Summary: {summary_text[:200]}"
        await self._update_agent_memory(agent, event)
        if other_agent:
            other_event = f"Thread in #{thread.channel} with {agent.agent_id} closed: {outcome}"
            if summary_text:
                other_event += f". Summary: {summary_text[:200]}"
            await self._update_agent_memory(other_agent, other_event)

    async def _sync_private_channels_from_db(self) -> None:
        """Discover collab_private channels created via the web-UI reopen flow.

        Queries ``agent_channels`` for rows with ``visibility='collab_private'``
        and integrates each new one into the engine state:

        - Adds to ``_channel_id_map`` and ``_channel_visibility``.
        - Adds the channel name to every member bot's ``subscribed_channels``
          (resolved from ``private_channel_members``), so Phase 2 scans it and
          Phase 4/5 can act in it.
        - Seeds a poll cursor so the first poll picks up the handover message.

        Cheap to call every main-loop tick — a single query returning a handful
        of rows. Idempotent: channels already known are skipped.
        """
        if not self.session_factory:
            return
        try:
            from sqlalchemy import select as sa_select
            from src.models import AgentChannel, PrivateChannelMember

            async with self.session_factory() as db:
                priv_rows = (await db.execute(
                    sa_select(AgentChannel).where(
                        AgentChannel.visibility == VISIBILITY_COLLAB_PRIVATE,
                        AgentChannel.archived_at.is_(None),
                    )
                )).scalars().all()

                # Integrate each channel we haven't seen yet.
                newly_discovered: list[AgentChannel] = []
                for ac in priv_rows:
                    if ac.channel_name in self._channel_id_map:
                        continue
                    self._channel_id_map[ac.channel_name] = ac.channel_id
                    self._channel_visibility[ac.channel_name] = VISIBILITY_COLLAB_PRIVATE
                    newly_discovered.append(ac)

                if not newly_discovered:
                    return

                # Load bot memberships for the newly-discovered channels.
                new_ids = [ac.id for ac in newly_discovered]
                members = (await db.execute(
                    sa_select(PrivateChannelMember).where(
                        PrivateChannelMember.agent_channel_id.in_(new_ids),
                        PrivateChannelMember.role == "bot",
                        PrivateChannelMember.removed_at.is_(None),
                    )
                )).scalars().all()

                by_channel: dict[uuid.UUID, list[str]] = {}
                for m in members:
                    if m.agent_id:
                        by_channel.setdefault(m.agent_channel_id, []).append(m.agent_id)

                for ac in newly_discovered:
                    bot_ids = by_channel.get(ac.id, [])
                    logger.info(
                        "Discovered private channel #%s (id=%s); subscribing bots: %s",
                        ac.channel_name, ac.channel_id, bot_ids,
                    )
                    for aid in bot_ids:
                        agent = self.agents.get(aid)
                        if agent:
                            agent.state.subscribed_channels.add(ac.channel_name)
                    # Record membership so polling/history calls for this
                    # private channel route through a bot that can see it.
                    self._private_channel_members[ac.channel_id] = set(bot_ids)
                    # Share channel name↔id with every client cache so post_message
                    # can resolve the name if one is passed.
                    for c in self.slack_clients.values():
                        c._channel_name_to_id[ac.channel_name] = ac.channel_id

            # Cursor rewind — always, for every known private channel (not only
            # newly-discovered ones), so this repairs any member bot whose
            # last_seen_cursor advanced past a private-channel message before
            # we'd subscribed them. Without this, the handover would be stuck
            # "in the past" relative to an agent's global scan cursor and
            # Phase 2 would skip it forever.
            self._rewind_cursors_for_private_channels()

        except Exception as exc:
            logger.warning("Failed to sync private channels from DB: %s", exc)

    def _rewind_cursors_for_private_channels(self) -> None:
        """Ensure each private channel's member bots have a cursor that lets
        them scan messages in that channel.

        For every tracked collab_private channel, find the oldest message in
        the in-memory log for that channel and rewind each member bot's
        last_seen_cursor to just before it (taking the min with its current
        value). No-op when the log has no messages for the channel yet (e.g.,
        discovery fired before rebuild/poll populated the log).
        """
        if not self._private_channel_members:
            return
        # channel_id -> oldest posted_at in the in-memory log.
        oldest_in_channel: dict[str, float] = {}
        for entry in self.message_log._entries:
            cid = self._channel_id_map.get(entry.channel)
            if not cid or cid not in self._private_channel_members:
                continue
            prev = oldest_in_channel.get(cid)
            if prev is None or entry.posted_at < prev:
                oldest_in_channel[cid] = entry.posted_at

        for cid, member_ids in self._private_channel_members.items():
            oldest = oldest_in_channel.get(cid)
            if oldest is None:
                continue
            rewind_to = oldest - 0.001  # just before, so "> cursor" includes it
            for aid in member_ids:
                agent = self.agents.get(aid)
                if not agent:
                    continue
                if agent.state.last_seen_cursor > rewind_to:
                    logger.info(
                        "[%s] Rewinding last_seen_cursor %.3f -> %.3f to scan private channel",
                        aid, agent.state.last_seen_cursor, rewind_to,
                    )
                    agent.state.last_seen_cursor = rewind_to

    def _resolve_channel_visibility(self, channel_name: str) -> str:
        """Look up the visibility class of a channel by its name.

        Backed by an in-memory map (``self._channel_visibility``) populated
        alongside ``self._channel_id_map`` at rebuild/bootstrap time. Defaults
        to VISIBILITY_PUBLIC when the channel is not tracked (e.g., seeded
        channels before their AgentChannel row is created).
        """
        return self._channel_visibility.get(channel_name, VISIBILITY_PUBLIC)

    def _get_prior_threads_for_agent(
        self,
        agent_id: str,
        current_visibility: str = VISIBILITY_PUBLIC,
    ) -> dict[str, list[dict]]:
        """Return {other_agent_id: [thread summaries]} visible at the given visibility level.

        Implements G3 (visibility-filtered dedup context): a thread_decision
        with ``origin_visibility='collab_private'`` never surfaces in a
        ``public``-channel Phase 5 prompt. See
        specs/privacy-and-channel-visibility.md §G3.
        """
        result: dict[str, list[dict]] = {}
        for (a, b), threads in self._prior_threads.items():
            if agent_id not in (a, b):
                continue
            other = b if a == agent_id else a
            visible = [
                t for t in threads
                if _visibility_permits(
                    t.get("origin_visibility", VISIBILITY_PUBLIC),
                    current_visibility,
                )
            ]
            if visible:
                result[other] = visible
        return result

    # ------------------------------------------------------------------
    # Phase 5: New Post (conditional)
    # ------------------------------------------------------------------

    async def _phase5_new_post(self, agent: Agent, phase4_thread_ids: set[str] | None = None) -> None:
        """Optionally start a new thread or reply to an interesting post."""
        settings = get_settings()
        phase4_thread_ids = phase4_thread_ids or set()

        # Daily post cap
        today_posts = self._count_today_posts(agent)
        if today_posts >= settings.daily_post_cap:
            logger.debug("[%s] Phase 5: Skipped (daily cap %d/%d)", agent.agent_id, today_posts, settings.daily_post_cap)
            return

        # Check preconditions
        at_thread_threshold = self._non_funding_thread_count(agent) >= settings.active_thread_threshold
        has_unreviewed_non_funding = any(
            not p.reviewed and not self.message_log.is_funding_thread(p.thread_id)
            for p in agent.state.pending_proposals
        )
        blocked_for_regular = at_thread_threshold or has_unreviewed_non_funding

        # Check for PI-priority posts — these bypass random skip and blocking
        has_pi_priority = any(p.pi_priority for p in agent.state.interesting_posts)

        if not has_pi_priority and random.random() < settings.phase5_skip_probability:
            logger.debug("[%s] Phase 5: Skipped (random)", agent.agent_id)
            return

        # Filter out interesting posts that are already active threads (replied in Phase 4)
        # or that already have a thread with another agent (2-party limit)
        available_posts = []
        for post in agent.state.interesting_posts:
            if post.post_id in phase4_thread_ids:
                continue
            if post.post_id in agent.state.active_threads:
                continue

            is_funding = self.message_log.is_funding_thread(post.post_id)

            # PI-priority and funding posts bypass regular blocking
            if blocked_for_regular and not is_funding and not post.pi_priority:
                continue

            # Check thread participation rules: if the post tags a specific agent,
            # only that agent can reply; otherwise generic 2-party rule applies
            allowed = self.message_log.get_thread_allowed_agents(post.post_id)
            if allowed and len(allowed) >= 2 and agent.agent_id not in allowed:
                logger.debug(
                    "[%s] Phase 5: Skipping post %s — not in allowed set %s",
                    agent.agent_id, post.post_id, allowed,
                )
                continue
            available_posts.append(post)

        # If blocked and no available posts to reply to, still allow Phase 5
        # so the agent can create funding collaboration posts (Option B)
        has_funding_interesting = any(
            self.message_log.is_funding_thread(p.post_id)
            for p in agent.state.interesting_posts
        )
        has_thread_foas = any(
            ts.foa_number for ts in agent.state.active_threads.values()
        )
        if not available_posts and blocked_for_regular and not has_funding_interesting and not has_thread_foas:
            logger.debug("[%s] Phase 5: Skipped (blocked, no funding/PI posts available)", agent.agent_id)
            return

        # Temporarily replace interesting_posts for prompt building
        original_posts = agent.state.interesting_posts
        agent.state.interesting_posts = available_posts

        # Build prompt — include agent's recent posts for dedup
        recent_entries = self.message_log.get_agent_top_level_posts(agent.agent_id, limit=10)
        recent_posts = [
            {"channel": e.channel, "content_snippet": e.content[:150]}
            for e in recent_entries
        ]

        # Pre-load cached FOA text for funding posts so Phase 5 has full context
        foa_contexts: dict[str, str] = {}
        funding_thread_summaries: dict[str, str] = {}
        for post in available_posts:
            if post.foa_number:
                foa_text = format_foa_for_prompt(post.foa_number)
                if foa_text:
                    foa_contexts[post.post_id] = foa_text
            if self.message_log.is_funding_thread(post.post_id):
                summary = summarize_funding_thread(
                    self.message_log, post.post_id, viewer_agent_id=agent.agent_id,
                )
                if not summary.is_empty():
                    funding_thread_summaries[post.post_id] = format_funding_thread_summary(summary)

        # Also pre-load FOAs from active/closed threads for Option B
        # (starting a new funding collab from a previously seen FOA)
        thread_foa_contexts: dict[str, str] = {}
        for ts in agent.state.active_threads.values():
            if ts.foa_number and ts.foa_number not in thread_foa_contexts:
                foa_text = format_foa_for_prompt(ts.foa_number)
                if foa_text:
                    thread_foa_contexts[ts.foa_number] = foa_text

        # Prior conversations for dedup — visibility-filtered per G3.
        # Phase 5 in v1 always runs against public channels (private channels
        # come into being via migration, not via Phase 5 new posts), so we pass
        # VISIBILITY_PUBLIC here. When/if Phase 5 ever runs against a private
        # channel, resolve the visibility from that channel's AgentChannel row.
        current_visibility = VISIBILITY_PUBLIC
        prior_threads = self._get_prior_threads_for_agent(
            agent.agent_id, current_visibility=current_visibility,
        )

        system_prompt, messages = agent.build_phase5_prompt(
            recent_posts=recent_posts,
            foa_contexts=foa_contexts,
            thread_foa_contexts=thread_foa_contexts,
            prior_threads=prior_threads,
            funding_only=blocked_for_regular,
            funding_thread_summaries=funding_thread_summaries,
            visibility=current_visibility,
        )

        # Restore
        agent.state.interesting_posts = original_posts

        agent.api_call_count += 1
        try:
            response = await generate_agent_response(
                system_prompt=system_prompt,
                messages=messages,
                model=settings.llm_agent_model_opus,
                max_tokens=1000,
                log_meta={"agent_id": agent.agent_id, "phase": "new_post"},
            )
            if not response or not response.strip():
                logger.warning("[%s] Phase 5: Empty response from LLM, skipping", agent.agent_id)
                return

            # Parse the JSON + message from the response
            action_data, message_text = self._parse_phase5_response(response)
            if not action_data:
                logger.warning("[%s] Phase 5: Could not parse response", agent.agent_id)
                return

            action = action_data.get("action", "new_post")
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

            # Real action — reset skip backoff
            agent.state.consecutive_phase5_skips = 0
            agent.state.last_phase5_action_time = time.time()

            channel = action_data.get("channel", "general").lstrip("#")
            target_post_id = action_data.get("target_post_id")
            post_type = action_data.get("post_type", "")

            # If agent is blocked, only allow funding-related actions
            if blocked_for_regular:
                is_funding_reply = (
                    action == "reply" and target_post_id
                    and self.message_log.is_funding_thread(target_post_id)
                )
                is_funding_post = post_type == "funding_collab"
                if not is_funding_reply and not is_funding_post:
                    logger.info(
                        "[%s] Phase 5: Blocked non-funding action while proposals pending",
                        agent.agent_id,
                    )
                    return

            # Retroactively add channel to the LLM log entry (unknown at call time)
            if self._llm_log_buffer:
                self._llm_log_buffer[-1]["channel"] = channel

            if action == "reply" and target_post_id:
                # Enforce thread participation rules
                allowed = self.message_log.get_thread_allowed_agents(target_post_id)
                if allowed and agent.agent_id not in allowed:
                    logger.info(
                        "[%s] Phase 5: Blocked reply to %s — not in allowed set %s",
                        agent.agent_id, target_post_id, allowed,
                    )
                    return

                # Funding-thread draft validators (atomic spin-off + no-ack rules)
                if self.message_log.is_funding_thread(target_post_id):
                    if is_announcement_only_funding_reply(message_text):
                        logger.info(
                            "[%s] Phase 5: Rejected announcement-only funding reply to %s",
                            agent.agent_id, target_post_id,
                        )
                        agent.state.consecutive_phase5_skips += 1
                        return
                    if is_acknowledgment_only_funding_reply(message_text):
                        logger.info(
                            "[%s] Phase 5: Rejected acknowledgment-only funding reply to %s",
                            agent.agent_id, target_post_id,
                        )
                        agent.state.consecutive_phase5_skips += 1
                        return

                # Reply to an interesting post → creates a new thread
                await self._post_message(
                    agent.agent_id, channel, message_text,
                    thread_ts=target_post_id,
                )
                agent.message_count += 1

                # Move from interesting_posts to active_threads
                agent.state.interesting_posts = [
                    p for p in agent.state.interesting_posts
                    if p.post_id != target_post_id
                ]
                # Determine the other agent from the original post
                original_entry = self.message_log.get_entry(target_post_id)
                other_id = original_entry.sender_agent_id if original_entry else None
                if other_id:
                    # Carry FOA number from the PostRef if this is a funding post
                    post_foa = None
                    for p in original_posts:
                        if p.post_id == target_post_id:
                            post_foa = p.foa_number
                            break
                    agent.state.active_threads[target_post_id] = ThreadState(
                        thread_id=target_post_id,
                        channel=channel,
                        other_agent_id=other_id,
                        message_count=2,  # original + this reply
                        foa_number=post_foa,
                    )

                logger.info(
                    "[%s] Phase 5: Replied to post %s in #%s",
                    agent.agent_id, target_post_id, channel,
                )

            else:
                # New top-level post
                await self._post_message(agent.agent_id, channel, message_text)
                agent.message_count += 1

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

        except Exception as exc:
            logger.error("[%s] Phase 5 failed: %s", agent.agent_id, exc)

    def _parse_phase5_response(self, response: str) -> tuple[dict | None, str | None]:
        """Parse Phase 5 response into (json_data, message_text).

        Expects JSON block + <slack_message> tags.  Uses the LAST JSON code
        block so that if the LLM revises its decision mid-response the final
        action wins.  Requires <slack_message> tags for the message body —
        raw text after the JSON block is never used (prevents reasoning leakage).
        """
        data = None
        try:
            # Find the LAST ```json``` block (LLM may revise mid-response)
            json_matches = list(
                re.finditer(r"```json\s*\n(.*?)\n```", response, re.DOTALL)
            )
            if json_matches:
                data = json.loads(json_matches[-1].group(1))
            else:
                # Try finding raw JSON
                json_start = response.find("{")
                json_end = response.find("}", json_start) + 1 if json_start >= 0 else -1
                if json_start >= 0 and json_end > json_start:
                    data = json.loads(response[json_start:json_end])
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Failed to parse Phase 5 JSON: %s", exc)

        if not data:
            return None, None

        # Extract message from <slack_message> tags (required — no raw-text fallback)
        msg_match = re.search(
            r"<slack_message>\s*(.*?)\s*</slack_message>", response, re.DOTALL
        )
        if msg_match:
            return data, msg_match.group(1).strip()

        return data, None

    # ------------------------------------------------------------------
    # Slack Polling (PI messages)
    # ------------------------------------------------------------------

    def _client_for_channel(self, channel_id: str, fallback):
        """Return a Slack client that can access ``channel_id``.

        For collab_private channels, picks a connected member bot (tracked in
        ``_private_channel_members``). For any other channel, returns the
        fallback (typically the round-robin poll client).

        Returns None if the channel is private and no connected member is
        available — the caller should skip the channel in that case.
        """
        members = self._private_channel_members.get(channel_id)
        if not members:
            return fallback
        for aid in members:
            client = self.slack_clients.get(aid)
            if client and client.is_connected:
                return client
        return None  # private, but no connected member

    def _next_poll_client(self):
        """Round-robin a connected Slack client for shared-token polling."""
        connected = [
            c for c in self.slack_clients.values() if c and c.is_connected
        ]
        if not connected:
            return None
        client = connected[self._poll_client_cursor % len(connected)]
        self._poll_client_cursor += 1
        return client

    async def _poll_slack_for_pi_messages(self) -> None:
        """
        Poll all channels for new human (non-bot) messages.
        Add them to the message log.
        """
        if not self.slack_clients:
            return

        now = time.time()
        if now - self._last_channel_poll < CHANNEL_POLL_INTERVAL:
            return
        self._last_channel_poll = now

        default_client = self._next_poll_client()
        if not default_client:
            return

        # Poll seeded channels plus any collab_private channels tracked in
        # _channel_visibility. Skipping non-seeded public channels avoids
        # polling archived/stale channels from prior sims.
        polled_ids = {
            ch_name: ch_id for ch_name, ch_id in self._channel_id_map.items()
            if ch_name in SEEDED_CHANNELS
            or self._channel_visibility.get(ch_name) == VISIBILITY_COLLAB_PRIVATE
        }
        for ch_name, ch_id in polled_ids.items():
            ch_visibility = self._channel_visibility.get(ch_name, VISIBILITY_PUBLIC)
            # Private channels need a member bot; non-members get channel_not_found.
            client = self._client_for_channel(ch_id, default_client)
            if client is None:
                logger.debug(
                    "Skipping poll for private channel #%s — no connected member bot",
                    ch_name,
                )
                continue
            oldest = self._poll_cursors.get(ch_id, "0")
            try:
                messages = client.poll_channel_messages(ch_id, oldest=oldest)
                for msg in messages:
                    ts = msg.get("ts", "")
                    user_id = msg.get("user", "")
                    is_bot = bool(msg.get("bot_id") or msg.get("subtype") == "bot_message")

                    if not is_bot and user_id:
                        is_bot = client.is_bot_user(user_id)

                    # Add bot messages to the log (so agents can scan them)
                    # but skip PI-specific handling for them
                    if is_bot:
                        bot_name = msg.get("username", "bot")
                        # Resolve agent_id from bot name
                        bot_agent_id = self.message_log._bot_name_to_id.get(
                            bot_name.lower()
                        )
                        entry = LogEntry(
                            ts=ts,
                            channel=ch_name,
                            sender_agent_id=bot_agent_id,
                            sender_name=bot_name,
                            content=msg.get("text", ""),
                            thread_ts=msg.get("thread_ts"),
                            posted_at=float(ts) if ts else 0.0,
                            is_bot=True,
                            visibility=ch_visibility,
                        )
                        if not self.message_log.get_entry(ts):
                            self.message_log.append(entry)
                        if ts:
                            self._poll_cursors[ch_id] = ts
                        continue

                    # Human message — resolve PI identity
                    sender_name = client.resolve_user_name(user_id)
                    pi_agent_ids = self._pi_slack_id_to_agent_ids.get(user_id, [])
                    entry = LogEntry(
                        ts=ts,
                        channel=ch_name,
                        sender_agent_id=None,
                        sender_name=sender_name,
                        content=msg.get("text", ""),
                        thread_ts=msg.get("thread_ts"),
                        posted_at=float(ts) if ts else 0.0,
                        is_bot=False,
                        visibility=ch_visibility,
                    )
                    self.message_log.append(entry)
                    logger.info(
                        "PI message in #%s from %s: %.60s",
                        ch_name, sender_name, msg.get("text", "")[:60],
                    )

                    # Check if PI message references a proposal (clears pending block)
                    self._check_pi_proposal_review(entry)

                    # PI-specific handling — apply to all agents this PI controls
                    for pi_agent_id in pi_agent_ids:
                      agent_obj = self.agents.get(pi_agent_id)
                      if agent_obj:
                          agent_obj.state.has_pi_directive = True
                      if not self._pi_handler:
                          continue
                      thread_ts = msg.get("thread_ts")

                      # PI posted in a closed thread → reopen it
                      if thread_ts and thread_ts in self._closed_thread_ids:
                          await self._reopen_thread(pi_agent_id, thread_ts, entry)

                      # PI posted in an active thread → set pi_context
                      elif thread_ts:
                          agent = self.agents.get(pi_agent_id)
                          if agent and thread_ts in agent.state.active_threads:
                              thread = agent.state.active_threads[thread_ts]
                              thread.pi_context = entry.content
                              thread.has_pending_reply = True
                              logger.info("[%s] PI posted in active thread %s", pi_agent_id, thread_ts)

                      # PI tagged their bot in a top-level post or reply
                      bot_name = self.agents[pi_agent_id].bot_name if pi_agent_id in self.agents else None
                      if bot_name and f"@{bot_name.lower()}" in msg.get("text", "").lower():
                          await self._pi_handler.handle_channel_tag(pi_agent_id, entry)

                    # Update cursor
                    if ts:
                        self._poll_cursors[ch_id] = ts

            except Exception as exc:
                logger.debug("Polling error for #%s: %s", ch_name, exc)

    def _check_pi_proposal_review(self, entry: LogEntry) -> None:
        """Check if a PI message clears a pending proposal for any agent."""
        thread_ts = entry.thread_ts
        if not thread_ts:
            return

        for agent in self.agents.values():
            for proposal in agent.state.pending_proposals:
                if proposal.thread_id == thread_ts and not proposal.reviewed:
                    proposal.reviewed = True
                    logger.info(
                        "[%s] Proposal in thread %s reviewed by PI",
                        agent.agent_id, thread_ts,
                    )

    async def _reopen_thread(self, agent_id: str, thread_ts: str, pi_entry: LogEntry) -> None:
        """Reopen a closed thread when a PI posts in it."""
        self._closed_thread_ids.discard(thread_ts)
        agent = self.agents.get(agent_id)
        if not agent:
            return

        # Find the other agent from thread history
        history = self.message_log.get_thread_history(thread_ts)
        other_id = None
        for entry in history:
            if entry.sender_agent_id and entry.sender_agent_id != agent_id:
                other_id = entry.sender_agent_id
                break

        if not other_id:
            logger.warning("[%s] Cannot reopen thread %s — no other agent found", agent_id, thread_ts)
            return

        # Create fresh ThreadState for both agents
        # Set message_count_offset so the bots get a fresh budget of replies
        existing_count = len(self.message_log.get_thread_history(thread_ts))
        agent.state.active_threads[thread_ts] = ThreadState(
            thread_id=thread_ts,
            channel=pi_entry.channel,
            other_agent_id=other_id,
            message_count=0,
            has_pending_reply=True,
            pi_context=pi_entry.content,
            message_count_offset=existing_count,
        )

        other_agent = self.agents.get(other_id)
        if other_agent:
            other_agent.state.active_threads[thread_ts] = ThreadState(
                thread_id=thread_ts,
                channel=pi_entry.channel,
                other_agent_id=agent_id,
                message_count=0,
                has_pending_reply=True,
                message_count_offset=existing_count,
            )

        logger.info("[%s] PI reopened closed thread %s with %s", agent_id, thread_ts, other_id)

    async def _poll_pi_dms(self) -> None:
        """Poll for DMs from PIs and process them via PIHandler."""
        if not self._pi_handler or not self._pi_slack_id_to_agent_ids:
            return

        # Default cursor to simulation start time — only process DMs sent after we started
        default_cursor = str(self._start_time.timestamp()) if self._start_time else "0"

        for pi_slack_id, agent_ids in self._pi_slack_id_to_agent_ids.items():
            for agent_id in agent_ids:
                client = self.slack_clients.get(agent_id)
                if not client or not client.is_connected:
                    continue

                oldest = self._dm_poll_cursors.get(agent_id, default_cursor)
                messages = client.poll_dm_messages(pi_slack_id, oldest=oldest)

                for msg in messages:
                    ts = msg.get("ts", "")
                    text = msg.get("text", "").strip()
                    if not text:
                        continue

                    logger.info("[%s] PI DM from %s: %s", agent_id, pi_slack_id, text[:80])

                    try:
                        await self._pi_handler.handle_dm(agent_id, pi_slack_id, text)
                        # Update working memory after PI interaction
                        agent = self.agents.get(agent_id)
                        if agent:
                            agent.state.has_pi_directive = True
                            await self._update_agent_memory(
                                agent, f"PI sent a DM: {text[:200]}"
                            )
                    except Exception as exc:
                        logger.error("[%s] Failed to handle PI DM: %s", agent_id, exc, exc_info=True)

                    # Update cursor to this message
                    if ts > oldest:
                        self._dm_poll_cursors[agent_id] = ts

    async def _poll_proposal_threads_for_pi(self) -> None:
        """Poll unreviewed proposal threads for PI replies.

        Thread replies don't appear in channel history, so this checks
        conversations.replies on each unreviewed proposal thread to detect
        PI messages that would trigger a thread reopen.
        """
        if not self._pi_slack_id_to_agent_ids:
            return

        now = time.time()
        if now - self._last_proposal_poll < PROPOSAL_POLL_INTERVAL:
            return
        self._last_proposal_poll = now

        # Collect PI user IDs for quick lookup
        pi_user_ids = set(self._pi_slack_id_to_agent_ids.keys())
        if not pi_user_ids:
            return

        # Find unreviewed proposals from in-memory state
        threads_to_poll: list[tuple[str, str, str]] = []  # (thread_id, channel_name, agent_id)
        seen = set()
        for agent in self.agents.values():
            for proposal in agent.state.pending_proposals:
                if not proposal.reviewed and proposal.thread_id not in seen:
                    seen.add(proposal.thread_id)
                    threads_to_poll.append(
                        (proposal.thread_id, proposal.channel, agent.agent_id)
                    )

        if not threads_to_poll:
            return

        client = self._next_poll_client()
        if not client:
            return

        default_cursor = str(self._start_time.timestamp()) if self._start_time else "0"

        for thread_id, channel_name, agent_id in threads_to_poll:
            ch_id = self._channel_id_map.get(channel_name)
            if not ch_id:
                continue

            cursor_key = f"proposal_thread:{thread_id}"
            oldest = self._poll_cursors.get(cursor_key, default_cursor)

            try:
                replies = client.get_thread_replies(ch_id, thread_id, oldest=oldest)
            except Exception as exc:
                logger.debug("Failed to poll proposal thread %s: %s", thread_id, exc)
                continue

            for msg in replies:
                ts = msg.get("ts", "")
                user_id = msg.get("user", "")

                # Skip bot messages and the root message
                if msg.get("bot_id") or ts == thread_id:
                    continue

                # Only process PI messages
                if user_id not in pi_user_ids:
                    continue

                sender_name = client.resolve_user_name(user_id)
                entry = LogEntry(
                    ts=ts,
                    channel=channel_name,
                    sender_agent_id=None,
                    sender_name=sender_name,
                    content=msg.get("text", ""),
                    thread_ts=thread_id,
                    posted_at=float(ts) if ts else 0.0,
                    is_bot=False,
                )

                # Avoid re-processing messages already in the log
                if self.message_log.get_entry(ts):
                    continue

                self.message_log.append(entry)
                logger.info(
                    "PI message in proposal thread %s (#%s) from %s: %.60s",
                    thread_id, channel_name, sender_name, msg.get("text", "")[:60],
                )

                # Mark proposal as reviewed
                self._check_pi_proposal_review(entry)

                # Reopen the thread for all PI's agents
                pi_agent_ids = self._pi_slack_id_to_agent_ids.get(user_id, [])
                for pi_agent_id in pi_agent_ids:
                    if thread_id in self._closed_thread_ids:
                        await self._reopen_thread(pi_agent_id, thread_id, entry)

                # Update cursor
                if ts > oldest:
                    self._poll_cursors[cursor_key] = ts

    # ------------------------------------------------------------------
    # Message posting
    # ------------------------------------------------------------------

    async def _post_message(
        self,
        agent_id: str,
        channel: str,
        text: str,
        thread_ts: str | None = None,
    ) -> None:
        """Post a message to Slack and record it in the message log + DB."""
        # Final safety: strip any leaked <slack_message> tags
        text = re.sub(r"</?slack_message>", "", text).strip()

        client = self.slack_clients.get(agent_id)
        agent = self.agents.get(agent_id)

        result = None
        if client and client.is_connected:
            result = client.post_message(channel, text, thread_ts=thread_ts)
        else:
            logger.info("[%s] MOCK post to #%s: %s...", agent_id, channel, text[:60])

        ts = result.get("ts", str(time.time())) if result else str(time.time())

        # Add to message log
        entry = LogEntry(
            ts=ts,
            channel=channel,
            sender_agent_id=agent_id,
            sender_name=agent.bot_name if agent else f"{agent_id}Bot",
            content=text,
            thread_ts=thread_ts,
            posted_at=float(ts) if ts else time.time(),
            is_bot=True,
        )
        self.message_log.append(entry)

        # Log to database
        if self.session_factory and self.simulation_run_id:
            await self._log_message(
                agent_id=agent_id,
                channel_id=result.get("channel", channel) if result else channel,
                channel_name=channel,
                message_ts=ts,
                thread_ts=thread_ts,
                message_length=len(text),
                phase="thread_reply" if thread_ts else "new_post",
            )

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    async def _load_pi_mappings(self) -> None:
        """Load PI and delegate Slack user ID -> agent ID mappings from AgentRegistry."""
        if not self.session_factory:
            logger.info("No DB session — skipping PI mapping load")
            return
        try:
            from sqlalchemy import select
            from src.models import AgentRegistry
            async with self.session_factory() as db:
                result = await db.execute(
                    select(
                        AgentRegistry.agent_id,
                        AgentRegistry.slack_user_id,
                        AgentRegistry.delegate_slack_ids,
                    )
                    .where(AgentRegistry.slack_user_id.isnot(None))
                    .where(AgentRegistry.status == "active")
                )
                for row in result:
                    # Primary PI
                    self._pi_slack_id_to_agent_ids.setdefault(row.slack_user_id, []).append(row.agent_id)
                    # Delegates
                    for delegate_id in (row.delegate_slack_ids or []):
                        self._pi_slack_id_to_agent_ids.setdefault(delegate_id, []).append(row.agent_id)
            if self._pi_slack_id_to_agent_ids:
                logger.info("Loaded PI mappings: %s", {
                    k[:8] + "...": v for k, v in self._pi_slack_id_to_agent_ids.items()
                })
            else:
                logger.info("No PI Slack accounts linked yet")
        except Exception as exc:
            logger.warning("Failed to load PI mappings: %s", exc)

    def _ensure_seeded_channels(self) -> None:
        """Create any missing seeded channels and join relevant bots."""
        client = next(iter(self.slack_clients.values()), None)
        if not client or not client.is_connected:
            # Mock mode — populate channel map with fake IDs
            self._channel_id_map = {ch: f"mock_{ch}" for ch in SEEDED_CHANNELS}
            # All seeded channels are public.
            self._channel_visibility = {ch: VISIBILITY_PUBLIC for ch in SEEDED_CHANNELS}
            return

        existing = client.list_channels()

        # Create missing seeded channels
        for ch_name in SEEDED_CHANNELS:
            if ch_name not in existing:
                logger.info("Creating seeded channel #%s", ch_name)
                ch_data = client.create_channel(ch_name)
                if ch_data:
                    existing[ch_name] = ch_data.get("id", "")

        self._channel_id_map = dict(existing)
        # Seeded channels are always 'public'. Agent-created channels (including
        # future collab_private channels) populate their own entries when the
        # agent_channels rows are loaded during engine-state rebuild.
        for ch_name in existing:
            self._channel_visibility.setdefault(ch_name, VISIBILITY_PUBLIC)

        # Join the first (polling) client to ALL seeded channels so it can poll them
        for ch_name, ch_id in existing.items():
            if ch_name in SEEDED_CHANNELS:
                client.join_channel(ch_id)

        # Share channel map across all clients
        for c in self.slack_clients.values():
            c._channel_name_to_id.update(existing)

    def _build_lab_directories(self) -> None:
        """Build a condensed publications directory for each agent (excluding their own lab)."""
        lab_pubs: dict[str, list[str]] = {}
        for agent in self.agents.values():
            profile_text = agent.public_profile
            match = re.search(
                r"## Recent Publications\n(.*?)(?=\n## |\Z)",
                profile_text,
                re.DOTALL,
            )
            if match:
                pubs = [
                    line.strip()
                    for line in match.group(1).strip().split("\n")
                    if line.strip().startswith("- ")
                ]
                if pubs:
                    lab_pubs[agent.agent_id] = pubs[:5]

        for agent in self.agents.values():
            sections = []
            for other_id, pubs in sorted(lab_pubs.items()):
                if other_id == agent.agent_id:
                    continue
                other_agent = self.agents[other_id]
                sections.append(f"### {other_agent.pi_name} Lab")
                sections.extend(pubs)
                sections.append("")
            agent._lab_directory = "\n".join(sections) if sections else None

    async def _backfill_foa_cache(self) -> None:
        """Ensure locally cached FOA details exist for all previously posted opportunities."""
        from src.agent.foa_cache import backfill_cache

        posted_path = Path("data/grantbot_posted.json")
        if not posted_path.exists():
            return
        try:
            posted_data = json.loads(posted_path.read_text(encoding="utf-8"))
            posted_numbers = [
                item.get("number", "") for item in posted_data if item.get("number")
            ]
            if posted_numbers:
                count = await backfill_cache(posted_numbers)
                if count:
                    logger.info("Backfilled FOA cache for %d opportunities", count)
        except Exception as exc:
            logger.warning("FOA cache backfill failed: %s", exc)

    async def _rebuild_state_from_slack(self) -> None:
        """Rebuild MessageLog and agent state from Slack history + DB."""
        default_client = next(iter(self.slack_clients.values()), None)
        if not default_client or not default_client.is_connected:
            logger.info("No Slack client available — skipping state rebuild")
            return

        # Build a mapping of bot_user_id -> agent_id
        bot_uid_to_agent: dict[str, str] = {}
        for aid, c in self.slack_clients.items():
            if c.bot_user_id:
                bot_uid_to_agent[c.bot_user_id] = aid

        # 1. Poll full Slack history for seeded channels + any known
        # collab_private channels. Same filter as the live-poll loop.
        polled_ids = {
            ch_name: ch_id for ch_name, ch_id in self._channel_id_map.items()
            if ch_name in SEEDED_CHANNELS
            or self._channel_visibility.get(ch_name) == VISIBILITY_COLLAB_PRIVATE
        }
        total_messages = 0
        total_threads = 0
        for ch_name, ch_id in polled_ids.items():
            ch_visibility = self._channel_visibility.get(ch_name, VISIBILITY_PUBLIC)
            # Route per-channel: private channels need a member bot.
            client = self._client_for_channel(ch_id, default_client)
            if client is None:
                logger.debug(
                    "Skipping rebuild for private channel #%s — no connected member bot",
                    ch_name,
                )
                continue
            messages = client.get_full_channel_history(ch_id)
            for msg in messages:
                ts = msg.get("ts", "")
                user_id = msg.get("user", "")
                bot_id = msg.get("bot_id")
                is_bot = bool(bot_id) or msg.get("subtype") == "bot_message"

                # Determine sender agent ID for bot messages
                sender_agent_id = None
                if is_bot and user_id:
                    sender_agent_id = bot_uid_to_agent.get(user_id)

                sender_name = msg.get("username", "") or user_id
                entry = LogEntry(
                    ts=ts,
                    channel=ch_name,
                    sender_agent_id=sender_agent_id,
                    sender_name=sender_name,
                    content=msg.get("text", ""),
                    thread_ts=msg.get("thread_ts") if msg.get("thread_ts") != ts else None,
                    posted_at=float(ts) if ts else 0.0,
                    is_bot=is_bot,
                    visibility=ch_visibility,
                )
                self.message_log.append(entry)
                total_messages += 1

                # Update poll cursor to latest
                if ts:
                    self._poll_cursors[ch_id] = ts

                # If this message has thread replies, fetch them
                reply_count = msg.get("reply_count", 0)
                if reply_count > 0:
                    replies = client.get_all_thread_replies(ch_id, ts)
                    total_threads += 1
                    for reply in replies:
                        rts = reply.get("ts", "")
                        if rts == ts:
                            continue  # skip parent (already added)
                        r_user_id = reply.get("user", "")
                        r_is_bot = bool(reply.get("bot_id")) or reply.get("subtype") == "bot_message"
                        r_agent_id = bot_uid_to_agent.get(r_user_id) if r_is_bot else None
                        r_entry = LogEntry(
                            ts=rts,
                            channel=ch_name,
                            sender_agent_id=r_agent_id,
                            sender_name=reply.get("username", "") or r_user_id,
                            content=reply.get("text", ""),
                            thread_ts=ts,
                            posted_at=float(rts) if rts else 0.0,
                            is_bot=r_is_bot,
                            visibility=ch_visibility,
                        )
                        self.message_log.append(r_entry)
                        total_messages += 1

        logger.info(
            "Rebuilt MessageLog: %d messages across %d channels, %d threads",
            total_messages, len(polled_ids), total_threads,
        )

        # 2. Rebuild active_threads per agent
        # Get all closed thread IDs and prior thread summaries from thread_decisions
        closed_thread_ids: set[str] = set()
        if self.session_factory:
            try:
                from sqlalchemy import select as sa_select
                async with self.session_factory() as db:
                    result = await db.execute(sa_select(ThreadDecision))
                    all_decisions = result.scalars().all()
                    for td in all_decisions:
                        closed_thread_ids.add(td.thread_id)
                        pair_key = tuple(sorted([td.agent_a, td.agent_b]))
                        self._prior_threads.setdefault(pair_key, []).append({
                            "channel": td.channel,
                            "outcome": td.outcome,
                            "summary": (td.summary_text or "")[:400] or None,
                            # Carried for G3 dedup-context visibility filtering.
                            "origin_visibility": td.origin_visibility,
                        })
                    self._closed_thread_ids.update(closed_thread_ids)
            except Exception as exc:
                logger.warning("Failed to load thread decisions: %s", exc)

        for agent in self.agents.values():
            aid = agent.agent_id
            # Find threads where this agent participated
            for entry in self.message_log._entries:
                if entry.sender_agent_id != aid:
                    continue
                thread_id = entry.thread_ts or entry.ts
                # Skip if already closed or already tracked
                if thread_id in closed_thread_ids:
                    continue
                if thread_id in agent.state.active_threads:
                    continue
                # Only count thread replies (not root posts without replies)
                if entry.thread_ts is None:
                    continue
                # Find the other agent in this thread
                root = self.message_log.get_entry(thread_id)
                if not root:
                    continue
                other_id = root.sender_agent_id if root.sender_agent_id != aid else None
                if not other_id:
                    # Check other replies for the other agent
                    history = self.message_log.get_thread_history(thread_id)
                    for h in history:
                        if h.sender_agent_id and h.sender_agent_id != aid:
                            other_id = h.sender_agent_id
                            break
                if not other_id:
                    continue

                msg_count = self.message_log.get_thread_message_count(thread_id)
                # Check if the last message was from the other agent (pending reply)
                history = self.message_log.get_thread_history(thread_id)
                last_sender = history[-1].sender_agent_id if history else None
                has_pending = last_sender is not None and last_sender != aid
                agent.state.active_threads[thread_id] = ThreadState(
                    thread_id=thread_id,
                    channel=entry.channel,
                    other_agent_id=other_id,
                    message_count=msg_count,
                    has_pending_reply=has_pending,
                )

        # 3. Rebuild pending_proposals per agent
        if self.session_factory:
            try:
                from sqlalchemy import select as sa_select
                async with self.session_factory() as db:
                    proposals_result = await db.execute(
                        sa_select(ThreadDecision).where(
                            ThreadDecision.outcome == "proposal"
                        )
                    )
                    proposals = proposals_result.scalars().all()

                    reviewed_result = await db.execute(
                        sa_select(
                            ProposalReview.thread_decision_id,
                            ProposalReview.agent_id,
                        )
                    )
                    reviewed_set = {
                        (r.thread_decision_id, r.agent_id) for r in reviewed_result
                    }

                # Keep only the latest ThreadDecision per (agent_id, thread_id).
                # Older rows represent prior propose/reopen cycles and their
                # reviews are stale — the most recent re-proposal is the only
                # one whose review status affects the agent's current block.
                latest_by_key: dict[tuple[str, str], ThreadDecision] = {}
                for td in proposals:
                    for aid in (td.agent_a, td.agent_b):
                        if aid not in self.agents:
                            continue
                        key = (aid, td.thread_id)
                        existing = latest_by_key.get(key)
                        if existing is None:
                            latest_by_key[key] = td
                            continue
                        td_ts = td.decided_at.timestamp() if td.decided_at else 0.0
                        ex_ts = existing.decided_at.timestamp() if existing.decided_at else 0.0
                        if td_ts > ex_ts:
                            latest_by_key[key] = td

                for (aid, _tid), td in latest_by_key.items():
                    agent = self.agents[aid]
                    is_reviewed = (td.id, aid) in reviewed_set
                    other = td.agent_b if aid == td.agent_a else td.agent_a
                    agent.state.pending_proposals.append(ProposalRef(
                        thread_id=td.thread_id,
                        channel=td.channel,
                        other_agent_id=other,
                        summary_text=td.summary_text or "",
                        proposed_at=td.decided_at.timestamp() if td.decided_at else 0.0,
                        reviewed=is_reviewed,
                    ))
            except Exception as exc:
                logger.warning("Failed to rebuild proposals: %s", exc)

        # 4. Rebuild api_call_count per agent from DB
        if self.session_factory and self.simulation_run_id:
            try:
                from sqlalchemy import func as sa_func
                from sqlalchemy import select as sa_select
                async with self.session_factory() as db:
                    result = await db.execute(
                        sa_select(
                            LlmCallLog.agent_id,
                            sa_func.count(LlmCallLog.id).label("count"),
                        )
                        .where(LlmCallLog.simulation_run_id == self.simulation_run_id)
                        .group_by(LlmCallLog.agent_id)
                    )
                    for r in result:
                        agent = self.agents.get(r.agent_id)
                        if agent:
                            agent.api_call_count = r.count
            except Exception as exc:
                logger.warning("Failed to rebuild api_call_count: %s", exc)

        # 5. Set last_seen_cursor per agent to latest message time
        if self._reset_cursors:
            logger.info("--reset-cursors: agents will re-scan all posts")
            for agent in self.agents.values():
                agent.state.last_seen_cursor = 0
        elif self.message_log._entries:
            latest_ts = max(e.posted_at for e in self.message_log._entries)
            for agent in self.agents.values():
                agent.state.last_seen_cursor = latest_ts

        # Log rebuild summary
        for agent in self.agents.values():
            at = len(agent.state.active_threads)
            pp = len(agent.state.pending_proposals)
            unrev = sum(1 for p in agent.state.pending_proposals if not p.reviewed)
            if at or pp:
                logger.info(
                    "[%s] Restored: %d active threads, %d proposals (%d unreviewed), %d API calls",
                    agent.agent_id, at, pp, unrev, agent.api_call_count,
                )

    def _infer_agent_id(self, name: str) -> str | None:
        """Try to infer agent_id from a bot name or display name."""
        name_lower = name.lower()
        # Direct lookup
        if name_lower in self._bot_name_to_id:
            return self._bot_name_to_id[name_lower]
        # Partial match
        for bot_name, agent_id in self._bot_name_to_id.items():
            if agent_id in name_lower or bot_name in name_lower:
                return agent_id
        return None

    # ------------------------------------------------------------------
    # LLM call logging
    # ------------------------------------------------------------------

    def _on_llm_call(self, data: dict) -> None:
        """Callback fired after each LLM API call."""
        self._llm_log_buffer.append(data)
        if len(self._llm_log_buffer) >= self._llm_log_flush_size:
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._flush_llm_logs())
                task.add_done_callback(self._on_flush_done)
            except RuntimeError:
                pass

    @staticmethod
    def _on_flush_done(task: asyncio.Task) -> None:
        if task.exception():
            logger.error("LLM log flush failed: %s", task.exception())

    async def _flush_llm_logs(self) -> None:
        """Write buffered LLM call logs to the database."""
        if not self._llm_log_buffer or not self.session_factory or not self.simulation_run_id:
            return
        batch = self._llm_log_buffer[:]
        self._llm_log_buffer.clear()
        try:
            async with self.session_factory() as db:
                for entry in batch:
                    record = LlmCallLog(
                        simulation_run_id=self.simulation_run_id,
                        agent_id=entry.get("agent_id", "unknown"),
                        phase=entry.get("phase", "unknown"),
                        channel=entry.get("channel"),
                        model=entry.get("model", ""),
                        system_prompt=entry.get("system_prompt", ""),
                        messages_json=entry.get("messages", []),
                        response_text=entry.get("response_text", ""),
                        input_tokens=entry.get("input_tokens", 0),
                        output_tokens=entry.get("output_tokens", 0),
                        latency_ms=entry.get("latency_ms", 0.0),
                        created_at=entry.get("completed_at"),
                    )
                    db.add(record)
                await db.commit()
            logger.debug("Flushed %d LLM call logs to DB", len(batch))
        except Exception as exc:
            logger.warning("Failed to flush LLM call logs: %s", exc)

    async def _sync_proposal_reviews_from_db(self) -> None:
        """Check DB for web-app proposal reviews and mark in-memory proposals as reviewed.

        For rating=0 reviews (reopened with PI guidance), also reopen the thread
        so both agents resume discussion incorporating the PI's direction.
        """
        if not self.session_factory:
            return
        try:
            async with self.session_factory() as db:
                from sqlalchemy import select as sa_select
                # Get all reviews with rating and guidance info, plus the
                # ThreadDecision's refined_in_channel marker (set when a
                # reopen migrated the refinement into a collab_private
                # channel — in that case the legacy "reopen the public
                # thread" path must NOT fire, or we'd drop the PI's
                # guidance back into the public thread and leak it).
                result = await db.execute(
                    sa_select(
                        ProposalReview.agent_id,
                        ProposalReview.rating,
                        ProposalReview.comment,
                        ThreadDecision.thread_id,
                        ThreadDecision.channel,
                        ThreadDecision.refined_in_channel,
                    )
                    .join(ThreadDecision, ProposalReview.thread_decision_id == ThreadDecision.id)
                )
                rows = list(result)

            reviewed_set = {(r.agent_id, r.thread_id) for r in rows}
            if not reviewed_set:
                return

            # Build lookup for rating=0 (reopened with guidance) reviews.
            # Rows with refined_in_channel set are skipped entirely — those
            # reopens were handled by the private-channel migration flow;
            # resurrecting the legacy public-thread reopen would undo the
            # privacy guarantee.
            reopen_guidance: dict[tuple[str, str], tuple[str, str]] = {}
            for r in rows:
                if r.rating == 0 and r.comment and r.refined_in_channel is None:
                    # Strip "[Reopened...] " prefix stored by the web/email route
                    guidance = r.comment
                    if guidance.startswith("[Reopened via email] "):
                        guidance = guidance[len("[Reopened via email] "):]
                    elif guidance.startswith("[Reopened] "):
                        guidance = guidance[len("[Reopened] "):]
                    reopen_guidance[(r.agent_id, r.thread_id)] = (guidance, r.channel)

            # Mark matching in-memory proposals as reviewed
            newly_reviewed: list[tuple[Agent, str]] = []
            for agent in self.agents.values():
                for proposal in agent.state.pending_proposals:
                    if not proposal.reviewed:
                        if (agent.agent_id, proposal.thread_id) in reviewed_set:
                            proposal.reviewed = True
                            newly_reviewed.append((agent, proposal.other_agent_id))
                            logger.info(
                                "[%s] Proposal for thread %s marked reviewed via web app",
                                agent.agent_id, proposal.thread_id,
                            )

            # Detect rating=0 reviews that need thread reopening, independent of
            # the reviewed flag (which may already be True from a prior sync).
            # Dedupe by thread_id within this pass so that if the PI reviewed
            # both sides of the pair, we only reopen the thread once.
            newly_reopened: list[tuple[Agent, str, str, str]] = []  # agent, other_id, thread_id, guidance
            seen_reopens: set[str] = set()
            for agent in self.agents.values():
                for proposal in agent.state.pending_proposals:
                    if proposal.thread_id in seen_reopens:
                        continue
                    if proposal.thread_id in self._db_reopened_thread_ids:
                        continue
                    key = (agent.agent_id, proposal.thread_id)
                    if key in reopen_guidance:
                        guidance, _channel = reopen_guidance[key]
                        seen_reopens.add(proposal.thread_id)
                        newly_reopened.append(
                            (agent, proposal.other_agent_id, proposal.thread_id, guidance)
                        )

            # Update memory for agents whose proposals were just reviewed
            for agent, other_id in newly_reviewed:
                event = f"PI reviewed proposal with {other_id} — agent is now unblocked for new posts"
                await self._update_agent_memory(agent, event)

            # Reopen threads where PI provided guidance (rating=0)
            for agent, other_id, thread_id, guidance in newly_reopened:
                channel = None
                for p in agent.state.pending_proposals:
                    if p.thread_id == thread_id:
                        channel = p.channel
                        break
                if not channel:
                    continue

                # Create a synthetic log entry for the PI guidance so it appears
                # in thread history and the agents can see it
                pi_entry = LogEntry(
                    ts=str(time.time()),
                    channel=channel,
                    sender_agent_id=None,
                    sender_name="PI (via web)",
                    content=guidance,
                    thread_ts=thread_id,
                    posted_at=time.time(),
                    is_bot=False,
                )
                self.message_log.append(pi_entry)

                # Reopen the thread for both agents
                self._closed_thread_ids.discard(thread_id)
                existing_count = len(self.message_log.get_thread_history(thread_id))

                agent.state.active_threads[thread_id] = ThreadState(
                    thread_id=thread_id,
                    channel=channel,
                    other_agent_id=other_id,
                    message_count=0,
                    has_pending_reply=True,
                    pi_context=guidance,
                    message_count_offset=existing_count,
                )

                other_agent = self.agents.get(other_id)
                if other_agent:
                    other_agent.state.active_threads[thread_id] = ThreadState(
                        thread_id=thread_id,
                        channel=channel,
                        other_agent_id=agent.agent_id,
                        message_count=0,
                        has_pending_reply=True,
                        message_count_offset=existing_count,
                    )

                self._db_reopened_thread_ids.add(thread_id)
                logger.info(
                    "[%s] PI guidance via web reopened thread %s with %s: %.60s",
                    agent.agent_id, thread_id, other_id, guidance[:60],
                )
        except Exception as exc:
            logger.debug("Proposal review sync failed: %s", exc)

    async def _log_message(
        self,
        agent_id: str,
        channel_id: str,
        channel_name: str,
        message_ts: str | None,
        thread_ts: str | None,
        message_length: int,
        phase: str,
    ) -> None:
        """Log an agent message to the database."""
        if not self.session_factory or not self.simulation_run_id:
            return
        try:
            async with self.session_factory() as db:
                record = AgentMessage(
                    simulation_run_id=self.simulation_run_id,
                    agent_id=agent_id,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    message_ts=message_ts,
                    thread_ts=thread_ts,
                    message_length=message_length,
                    phase=phase,
                )
                db.add(record)
                # Update run totals
                from sqlalchemy import select
                run_result = await db.execute(
                    select(SimulationRun).where(
                        SimulationRun.id == self.simulation_run_id
                    )
                )
                run = run_result.scalar_one_or_none()
                if run:
                    run.total_messages = (run.total_messages or 0) + 1
                    run.total_api_calls = sum(
                        a.api_call_count for a in self.agents.values()
                    )
                await db.commit()
        except Exception as exc:
            logger.warning("Failed to log message: %s", exc)

    # ------------------------------------------------------------------
    # Post-simulation
    # ------------------------------------------------------------------

    async def _update_agent_memory(
        self,
        agent: Agent,
        event: str,
        visibility: str = VISIBILITY_PUBLIC,
        channel_id: str | None = None,
    ) -> None:
        """Incrementally update an agent's working memory after a significant event.

        Triggered by: thread closure, PI DM, or proposal review — not batched at
        simulation end.

        visibility/channel_id: controls which memory segment is updated and
            which subset of the message log is used as synthesis context, per
            G2. v1 callers always pass public (the default); the
            thread-closure path will pass the thread's visibility once
            private-channel migration lands.
        """
        try:
            # Gather recent activity for context — filter the message log to
            # entries with matching visibility. Public syntheses never see
            # private-channel messages, and vice-versa. See §G2.
            agent_entries = [
                e for e in self.message_log._entries
                if e.sender_agent_id == agent.agent_id
                and e.visibility == visibility
            ]
            messages_text = "\n".join(
                f"[#{e.channel}] {e.content[:200]}"
                for e in agent_entries[-20:]
            ) if agent_entries else "(no recent messages)"

            system_prompt = agent.build_thread_reply_system_prompt(
                visibility=visibility, channel_id=channel_id,
            )
            messages = [
                {
                    "role": "user",
                    "content": f"""Update your working memory. The event that triggered this update:
{event}

Your recent messages for context:
{messages_text}

Your current working memory:
{agent.working_memory or "(empty)"}

Write the complete updated working memory. Incorporate the new event, keep existing
entries that are still relevant, and remove anything outdated. Summarize:
(a) Collaboration opportunities and their status
(b) Feedback or directions from your PI (if any)
(c) Current priorities

Keep it concise — under 300 words.""",
                }
            ]

            agent.api_call_count += 1
            response = await generate_agent_response(
                system_prompt=system_prompt,
                messages=messages,
                max_tokens=800,
                log_meta={"agent_id": agent.agent_id, "phase": "memory"},
            )
            if not response or not response.strip():
                logger.warning("[%s] Memory update: empty response", agent.agent_id)
                return
            agent.update_working_memory_file(
                response, visibility=visibility, channel_id=channel_id,
            )
            logger.info(
                "[%s] Working memory updated (visibility=%s, trigger: %s)",
                agent.agent_id, visibility, event[:60],
            )

            # Record revision
            if self.session_factory:
                try:
                    from sqlalchemy import select as sa_sel
                    from src.models import AgentRegistry
                    from src.services.profile_versioning import create_revision
                    async with self.session_factory() as db:
                        agent_reg = (await db.execute(
                            sa_sel(AgentRegistry)
                            .where(AgentRegistry.agent_id == agent.agent_id)
                        )).scalar_one_or_none()
                        if agent_reg:
                            await create_revision(
                                db,
                                agent_registry_id=agent_reg.id,
                                profile_type="memory",
                                content=response,
                                mechanism="agent",
                                change_summary=event[:200],
                            )
                            await db.commit()
                except Exception as rev_exc:
                    logger.warning("[%s] Profile revision failed: %s", agent.agent_id, rev_exc)
        except Exception as exc:
            logger.error("[%s] Working memory update failed: %s", agent.agent_id, exc)


def _extract_slack_message(text: str) -> str:
    """Extract the message from <slack_message> tags if present, else fall back to preamble stripping."""
    match = re.search(r"<slack_message>\s*(.*?)\s*</slack_message>", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Fallback: strip preamble heuristically
    return _strip_llm_preamble(text)


def _strip_llm_preamble(text: str) -> str:
    """Remove LLM internal reasoning that leaks before the actual Slack message.

    Strategy: split into paragraphs, identify the first paragraph that looks like
    an actual Slack message (not meta-commentary), and discard everything before it.
    """
    # If there's a --- separator, take everything after the last one
    if "\n---\n" in text:
        parts = text.split("\n---\n")
        candidate = parts[-1].strip()
        if candidate:
            text = candidate

    # Split into paragraphs (separated by blank lines)
    paragraphs = re.split(r"\n\s*\n", text.strip())
    if len(paragraphs) <= 1:
        return text

    # Patterns that indicate internal reasoning / meta-commentary
    _PREAMBLE_RE = re.compile(
        r"^("
        r"(That('s| is) (not|exactly|interesting))"
        r"|Let me"
        r"|I('ll| should| need| couldn't| didn't| can't| wasn't| don't| have| want)"
        r"|Now I (have|can|know|need|should)"
        r"|These |The (search|result|profile|paper|abstract|tool|API|PubMed|query)"
        r"|My (search|query|tool|approach)"
        r"|Based on|After (review|search|look)|Since (the|I|my)"
        r"|Looking at|It seems|Ok[,.]|Okay[,.]|Hmm"
        r"|This (is|gives|shows|confirms|doesn't|isn't)"
        r"|None of|No (relevant|useful|results)"
        r"|Unfortunately"
        r")",
        re.IGNORECASE,
    )

    # Find the first non-preamble paragraph
    for i, para in enumerate(paragraphs):
        first_line = para.strip().split("\n")[0]
        if not _PREAMBLE_RE.match(first_line):
            if i > 0:
                stripped = "\n\n".join(paragraphs[i:]).strip()
                logger.info(
                    "Stripped %d preamble paragraph(s): %.120s",
                    i, " | ".join(p.strip()[:50] for p in paragraphs[:i]),
                )
                return stripped
            break

    return text


def _extract_json(text: str) -> dict[str, Any]:
    """Extract JSON from LLM response text."""
    text = text.strip()
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    if "```json" in text:
        start = text.find("```json") + 7
        end = text.find("```", start)
        if end > start:
            try:
                return json.loads(text[start:end].strip())
            except json.JSONDecodeError:
                pass
    if "```" in text:
        start = text.find("```") + 3
        end = text.find("```", start)
        if end > start:
            try:
                return json.loads(text[start:end].strip())
            except json.JSONDecodeError:
                pass
    start = text.find("{")
    end = text.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end])
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Could not extract JSON from response: {text[:200]}")
