"""Turn-based simulation engine — coordinates all agents across all channels."""

import asyncio
import json
import logging
import random
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from src.agent.agent import Agent
from src.agent.channels import SEEDED_CHANNELS
from src.agent.message_log import LogEntry, MessageLog
from src.agent.state import PostRef, ProposalRef, ThreadState
from src.agent.tools import TOOL_DEFINITIONS, execute_tool
from src.config import get_settings
from src.models import AgentMessage, LlmCallLog, SimulationRun, ThreadDecision
from src.services.llm import (
    generate_agent_response,
    generate_with_tools,
    set_call_log_callback,
)

logger = logging.getLogger(__name__)

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
    ):
        self.agents = {a.agent_id: a for a in agents}
        self.slack_clients = slack_clients
        self.max_runtime_minutes = max_runtime_minutes
        self.budget_cap = budget_cap
        self.session_factory = session_factory
        self.simulation_run_id = simulation_run_id

        self._start_time: datetime | None = None
        self._running = False
        self.message_log = MessageLog()

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

        # Slack poll cursor: channel_id -> latest ts seen
        self._poll_cursors: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def is_within_time_limit(self) -> bool:
        if not self._start_time:
            return True
        elapsed = (datetime.now(timezone.utc) - self._start_time).total_seconds()
        return elapsed < self.max_runtime_minutes * 60

    def _agent_within_budget(self, agent: Agent) -> bool:
        return agent.api_call_count < self.budget_cap

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
        self._build_lab_directories()
        set_call_log_callback(self._on_llm_call)

        # Main loop
        turn_count = 0
        while self._running and self.is_within_time_limit:
            # Poll Slack for PI messages
            await self._poll_slack_for_pi_messages()

            # Select agent
            agent = self._select_agent()
            if not agent or not self._agent_within_budget(agent):
                # All agents over budget
                logger.info("All agents over budget or no agent selected. Stopping.")
                break

            logger.info("=== Turn %d: %s ===", turn_count + 1, agent.agent_id)

            # Run 5-phase turn
            try:
                await self._run_turn(agent)
            except Exception:
                logger.exception("Error during turn for %s", agent.agent_id)

            # Update last_selected
            agent.state.last_selected = time.time()
            turn_count += 1

            # Optional delay
            if settings.turn_delay_seconds > 0:
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
        """Weighted random selection: P(agent) ∝ (now - agent.last_selected)."""
        now = time.time()
        candidates = [
            a for a in self.agents.values()
            if self._agent_within_budget(a)
        ]
        if not candidates:
            return None

        weights = [max(now - a.state.last_selected, 1.0) for a in candidates]
        return random.choices(candidates, weights=weights, k=1)[0]

    # ------------------------------------------------------------------
    # Turn execution (5 phases)
    # ------------------------------------------------------------------

    async def _run_turn(self, agent: Agent) -> None:
        """Run all 5 phases for a single agent turn."""
        # Phase 1: Channel discovery
        self._phase1_channel_discovery(agent)

        # Phase 2: Scan & filter new posts
        await self._phase2_scan_filter(agent)

        # Phase 3: Activate threads from tags and replies
        self._phase3_activate_threads(agent)

        # Phase 4: Reply to active threads (parallel)
        phase4_thread_ids = await self._phase4_reply_threads(agent)

        # Phase 5: Start new thread (conditional)
        await self._phase5_new_post(agent, phase4_thread_ids)

        # Update cursor
        agent.state.last_seen_cursor = time.time()

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
                    agent.state.interesting_posts.append(PostRef(
                        post_id=post.ts,
                        channel=post.channel,
                        sender_agent_id=post.sender_agent_id or post.sender_name,
                        content_snippet=post.content[:200],
                        posted_at=post.posted_at,
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
            if len(agent.state.active_threads) >= settings.active_thread_threshold:
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
                agent.state.active_threads[thread_id] = ThreadState(
                    thread_id=thread_id,
                    channel=entry.channel,
                    other_agent_id=other_id,
                    message_count=self.message_log.get_thread_message_count(thread_id),
                    has_pending_reply=True,
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
            if len(agent.state.active_threads) >= settings.active_thread_threshold:
                break
            # Check thread participation rules
            allowed = self.message_log.get_thread_allowed_agents(thread_id)
            if allowed and len(allowed) >= 2 and agent.agent_id not in allowed:
                continue
            other_id = self._infer_agent_id(entry.sender_name) or entry.sender_agent_id
            if other_id and other_id != agent.agent_id:
                agent.state.active_threads[thread_id] = ThreadState(
                    thread_id=thread_id,
                    channel=entry.channel,
                    other_agent_id=other_id,
                    message_count=self.message_log.get_thread_message_count(thread_id),
                    has_pending_reply=True,
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

        # Update message count
        thread.message_count = len(history_entries)

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

        # Build prompt
        system_prompt, messages = agent.build_phase4_prompt(
            thread=thread,
            thread_history=thread_history,
            other_agent_name=other_name,
            other_agent_lab=other_lab,
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
                max_tokens=800,
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

            # Post the reply
            await self._post_message(
                agent.agent_id, thread.channel, response_text,
                thread_ts=thread.thread_id,
            )
            agent.message_count += 1
            thread.has_pending_reply = False

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
            # Look back in thread history for a :memo: Summary from the other agent
            history = self.message_log.get_thread_history(thread.thread_id)
            for entry in history:
                if entry.sender_agent_id == thread.other_agent_id and ":memo:" in entry.content:
                    # Proposal confirmed!
                    logger.info(
                        "[%s] Thread %s: proposal confirmed with ✅",
                        agent.agent_id, thread.thread_id,
                    )
                    summary_text = entry.content
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

        # Check for graceful close indicators
        close_phrases = [
            "not enough overlap",
            "insufficient overlap",
            "don't see a concrete",
            "no clear first experiment",
            "too parallel",
            "wishing you",
            "best of luck",
        ]
        if any(phrase in latest_reply.lower() for phrase in close_phrases):
            logger.info(
                "[%s] Thread %s: graceful close detected",
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
        # Remove from active threads
        agent.state.active_threads.pop(thread.thread_id, None)

        # Also close for the other agent if they have this thread active
        other_agent = self.agents.get(thread.other_agent_id)
        if other_agent and thread.thread_id in other_agent.state.active_threads:
            other_agent.state.active_threads[thread.thread_id].status = "closed"
            other_agent.state.active_threads.pop(thread.thread_id, None)
            # If proposal, add to other agent's pending_proposals too
            if outcome == "proposal" and summary_text:
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

    # ------------------------------------------------------------------
    # Phase 5: New Post (conditional)
    # ------------------------------------------------------------------

    async def _phase5_new_post(self, agent: Agent, phase4_thread_ids: set[str] | None = None) -> None:
        """Optionally start a new thread or reply to an interesting post."""
        settings = get_settings()
        phase4_thread_ids = phase4_thread_ids or set()

        # Check preconditions
        if len(agent.state.active_threads) >= settings.active_thread_threshold:
            logger.debug("[%s] Phase 5: Skipped (at thread threshold)", agent.agent_id)
            return

        if any(not p.reviewed for p in agent.state.pending_proposals):
            logger.debug("[%s] Phase 5: Skipped (pending proposal)", agent.agent_id)
            return

        if random.random() < settings.phase5_skip_probability:
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

        # Temporarily replace interesting_posts for prompt building
        original_posts = agent.state.interesting_posts
        agent.state.interesting_posts = available_posts

        # Build prompt
        system_prompt, messages = agent.build_phase5_prompt()

        # Restore
        agent.state.interesting_posts = original_posts

        agent.api_call_count += 1
        try:
            response = await generate_agent_response(
                system_prompt=system_prompt,
                messages=messages,
                model=settings.llm_agent_model_opus,
                max_tokens=600,
                log_meta={"agent_id": agent.agent_id, "phase": "new_post"},
            )
            if not response or not response.strip():
                logger.warning("[%s] Phase 5: Empty response from LLM, skipping", agent.agent_id)
                return

            # Parse the JSON + message from the response
            action_data, message_text = self._parse_phase5_response(response)
            if not action_data or not message_text:
                logger.warning("[%s] Phase 5: Could not parse response", agent.agent_id)
                return

            action = action_data.get("action", "new_post")
            channel = action_data.get("channel", "general").lstrip("#")
            target_post_id = action_data.get("target_post_id")

            if action == "reply" and target_post_id:
                # Enforce thread participation rules
                allowed = self.message_log.get_thread_allowed_agents(target_post_id)
                if allowed and agent.agent_id not in allowed:
                    logger.info(
                        "[%s] Phase 5: Blocked reply to %s — not in allowed set %s",
                        agent.agent_id, target_post_id, allowed,
                    )
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
                    agent.state.active_threads[target_post_id] = ThreadState(
                        thread_id=target_post_id,
                        channel=channel,
                        other_agent_id=other_id,
                        message_count=2,  # original + this reply
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
        """Parse Phase 5 response into (json_data, message_text)."""
        try:
            # Find JSON block
            json_match = re.search(r"```json\s*\n(.*?)\n```", response, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
                data = json.loads(json_str)
                # Message text is everything after the JSON block
                rest = response[json_match.end():].strip()
                return data, rest

            # Try finding raw JSON at the start
            json_start = response.find("{")
            json_end = response.find("}", json_start) + 1 if json_start >= 0 else -1
            if json_start >= 0 and json_end > json_start:
                data = json.loads(response[json_start:json_end])
                rest = response[json_end:].strip()
                return data, rest

        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Failed to parse Phase 5 JSON: %s", exc)

        return None, None

    # ------------------------------------------------------------------
    # Slack Polling (PI messages)
    # ------------------------------------------------------------------

    async def _poll_slack_for_pi_messages(self) -> None:
        """
        Poll all channels for new human (non-bot) messages.
        Add them to the message log.
        """
        if not self.slack_clients:
            return

        # Use first available client to poll
        client = next(iter(self.slack_clients.values()), None)
        if not client or not client.is_connected:
            return

        # Only poll seeded channels (not archived/stale channels from prior sims)
        seeded_ids = {
            ch_name: ch_id for ch_name, ch_id in self._channel_id_map.items()
            if ch_name in SEEDED_CHANNELS
        }
        for ch_name, ch_id in seeded_ids.items():
            oldest = self._poll_cursors.get(ch_id, "0")
            try:
                messages = client.poll_channel_messages(ch_id, oldest=oldest)
                for msg in messages:
                    ts = msg.get("ts", "")
                    user_id = msg.get("user", "")

                    # Skip bot messages — we only want human PI messages here
                    if msg.get("bot_id") or msg.get("subtype") == "bot_message":
                        continue

                    # Check if this user is a bot
                    if user_id and client.is_bot_user(user_id):
                        continue

                    # Human message — add to log
                    sender_name = client.resolve_user_name(user_id)
                    entry = LogEntry(
                        ts=ts,
                        channel=ch_name,
                        sender_agent_id=None,
                        sender_name=sender_name,
                        content=msg.get("text", ""),
                        thread_ts=msg.get("thread_ts"),
                        posted_at=float(ts) if ts else 0.0,
                        is_bot=False,
                    )
                    self.message_log.append(entry)
                    logger.info(
                        "PI message in #%s from %s: %.60s",
                        ch_name, sender_name, msg.get("text", "")[:60],
                    )

                    # Check if PI message references a proposal (clears pending block)
                    self._check_pi_proposal_review(entry)

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

    def _ensure_seeded_channels(self) -> None:
        """Create any missing seeded channels and join relevant bots."""
        client = next(iter(self.slack_clients.values()), None)
        if not client or not client.is_connected:
            # Mock mode — populate channel map with fake IDs
            self._channel_id_map = {ch: f"mock_{ch}" for ch in SEEDED_CHANNELS}
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

        pub_count = sum(len(p) for p in lab_pubs.values())
        logger.info(
            "Built lab directories: %d labs with %d total publications",
            len(lab_pubs), pub_count,
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

    async def update_all_working_memories(self) -> None:
        """Update working memory for all agents after the simulation."""
        for agent in self.agents.values():
            try:
                # Build summary of agent's interactions
                agent_entries = [
                    e for e in self.message_log._entries
                    if e.sender_agent_id == agent.agent_id
                ]
                if not agent_entries:
                    continue

                messages_text = "\n".join(
                    f"[#{e.channel}] {e.content[:200]}"
                    for e in agent_entries[:30]
                )

                system_prompt = agent.build_system_prompt()
                messages = [
                    {
                        "role": "user",
                        "content": f"""Based on your recent conversations, update your working memory.

Your recent messages:
{messages_text}

Write an updated working memory summarizing:
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
                    max_tokens=400,
                    log_meta={"agent_id": agent.agent_id, "phase": "memory"},
                )
                if not response or not response.strip():
                    logger.warning("[%s] Working memory update: empty response", agent.agent_id)
                    continue
                agent.update_working_memory_file(response)
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
