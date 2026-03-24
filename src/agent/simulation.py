"""Simulation engine — coordinates all agents across all channels."""

import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Any

from src.agent.agent import Agent
from src.agent.channels import SEEDED_CHANNELS
from src.config import get_settings
from src.models import AgentMessage, LlmCallLog, SimulationRun
from src.services.llm import set_call_log_callback

logger = logging.getLogger(__name__)

# response_type values that route to Opus
OPUS_RESPONSE_TYPES = {"collaboration", "experiment", "help_wanted", "summary"}

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


class SimulationEngine:
    """
    Coordinates all agents across Slack channels.
    Manages timing, budget, kickstart, and response sequencing.
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
        self._message_queue: asyncio.Queue = asyncio.Queue()

        # Channel history cache: channel_name -> list of messages
        self._channel_history: dict[str, list[dict]] = {}
        # Channel ID to name mapping
        self._channel_id_map: dict[str, str] = {}

        # LLM call log buffer
        self._llm_log_buffer: list[dict] = []
        self._llm_log_flush_size = 10

        # Thread discipline tracking: thread_ts -> {op: agent_id, participants: set, op_replied: bool}
        self._thread_meta: dict[str, dict] = {}

        # Top-level post tracking: (agent_id, channel_name) -> count
        self._toplevel_posts: dict[tuple[str, str], int] = {}
        self.max_toplevel_per_channel = 1  # max new threads per agent per channel per simulation day

        # Track which channels each agent joined: agent_id -> set of channel names
        self._agent_channels: dict[str, set[str]] = {}

        # Deduplication: track message timestamps already processed
        self._seen_message_ts: set[str] = set()

    @property
    def is_within_time_limit(self) -> bool:
        if not self._start_time:
            return True
        elapsed = (datetime.now(timezone.utc) - self._start_time).total_seconds()
        return elapsed < self.max_runtime_minutes * 60

    def _agent_within_budget(self, agent: Agent) -> bool:
        return agent.api_call_count < self.budget_cap

    async def start(self) -> None:
        """Start the simulation."""
        self._start_time = datetime.now(timezone.utc)
        self._running = True
        logger.info(
            "Simulation started. Max runtime: %dm, Budget: %d calls/agent",
            self.max_runtime_minutes,
            self.budget_cap,
        )

        # Ensure all seeded channels exist
        self._ensure_seeded_channels()

        # Build lab directory (other labs' recent publications) for each agent
        self._build_lab_directories()

        # Register LLM call log callback
        set_call_log_callback(self._on_llm_call)

        # Register message handlers — capture the event loop now since Slack Bolt
        # handlers run in thread pool threads that don't have an event loop.
        loop = asyncio.get_running_loop()
        handler_count = 0
        for agent_id, client in self.slack_clients.items():
            if hasattr(client, "on_message"):
                def make_handler(aid):
                    def handler(msg):
                        logger.debug(
                            "Event from %s bot: sender=%s channel=%s",
                            aid, msg.get("sender", "?"), msg.get("channel_name", "?"),
                        )
                        asyncio.run_coroutine_threadsafe(
                            self._message_queue.put(msg),
                            loop,
                        )
                    return handler
                client.on_message = make_handler(agent_id)
                handler_count += 1
        logger.info("Registered message handlers for %d agents", handler_count)

        # Run kickstart and message processing concurrently
        await asyncio.gather(
            self._run_kickstart(),
            self._process_messages(),
        )

    async def stop(self) -> None:
        """Stop the simulation gracefully."""
        self._running = False
        set_call_log_callback(None)
        await self._flush_llm_logs()
        logger.info("Simulation stopping...")

    def _build_lab_directories(self) -> None:
        """Build a condensed publications directory for each agent (excluding their own lab)."""
        import re
        from pathlib import Path

        # Parse publications from each agent's profile
        lab_pubs: dict[str, list[str]] = {}
        for agent in self.agents.values():
            profile_text = agent.public_profile
            # Extract the "Recent Publications" section
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
                    lab_pubs[agent.agent_id] = pubs[:5]  # Top 5 per lab

        # For each agent, build directory of OTHER labs' papers
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
            len(lab_pubs),
            pub_count,
        )

    def _on_llm_call(self, data: dict) -> None:
        """Callback fired after each LLM API call. Buffers for batch DB write."""
        self._llm_log_buffer.append(data)
        if len(self._llm_log_buffer) >= self._llm_log_flush_size:
            # Schedule flush without blocking the caller
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._flush_llm_logs())
                task.add_done_callback(self._on_flush_done)
            except RuntimeError:
                pass  # No event loop — will flush on stop()

    @staticmethod
    def _on_flush_done(task: asyncio.Task) -> None:
        """Log any exceptions from flush tasks."""
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

    @staticmethod
    def _select_response_model(decision: dict) -> str:
        """Select Opus or Sonnet based on the decision's response_type."""
        settings = get_settings()
        response_type = decision.get("response_type", "follow_up")
        if response_type in OPUS_RESPONSE_TYPES:
            return settings.llm_agent_model_opus
        return settings.llm_agent_model_sonnet

    async def _run_kickstart(self) -> None:
        """Post scripted and generated kickstart messages to initiate conversation."""
        kickstart_config = self._load_kickstart_config()

        # Handle nested YAML structure (kickstart: { scripted: [...] })
        if "kickstart" in kickstart_config and "scripted" not in kickstart_config:
            kickstart_config = kickstart_config["kickstart"]

        # Shuffle scripted openers so the same bot doesn't always go first
        scripted = list(kickstart_config.get("scripted", []))
        random.shuffle(scripted)

        for opener in scripted:
            agent_id = opener.get("agent")
            channel = opener.get("channel", "general").lstrip("#")
            message = opener.get("message", "")

            if not message or agent_id not in self.agents:
                continue

            await asyncio.sleep(random.uniform(5, 30))
            await self._post_message(agent_id, channel, message)

        # Generated openers for remaining agents (randomized)
        scripted_agents = {o.get("agent") for o in scripted}
        remaining_agents = [a for a in self.agents.values() if a.agent_id not in scripted_agents]
        random.shuffle(remaining_agents)

        for agent in remaining_agents:
            if not self.is_within_time_limit:
                break
            await asyncio.sleep(random.uniform(10, 60))
            try:
                # Pick a channel this agent hasn't posted a top-level message to yet
                candidates = [ch for ch in ["general", "drug-repurposing", "structural-biology",
                                            "aging-and-longevity", "chemical-biology"]
                              if self._can_post_toplevel(agent.agent_id, ch)]
                if not candidates:
                    logger.debug("[%s] No channels available for kickstart (all at limit)", agent.agent_id)
                    continue
                channel = random.choice(candidates)
                message = await agent.generate_kickstart_message(channel)
                await self._post_message(agent.agent_id, channel, message)
            except Exception as exc:
                logger.error("[%s] Kickstart failed: %s", agent.agent_id, exc)
        logger.info("Kickstart phase complete. %d scripted, %d generated openers posted.",
                     len(scripted), len(remaining_agents))

    def _load_kickstart_config(self) -> dict:
        """Load kickstart configuration from prompts/agent-kickstart.md."""
        import yaml
        from pathlib import Path
        try:
            text = (Path("prompts") / "agent-kickstart.md").read_text()
            # Extract YAML block
            if "```yaml" in text:
                start = text.find("```yaml") + 7
                end = text.find("```", start)
                yaml_text = text[start:end].strip()
                return yaml.safe_load(yaml_text) or {}
        except Exception:
            pass
        return {"scripted": _default_kickstart_messages()}

    async def _process_messages(self) -> None:
        """Process incoming messages from the queue."""
        logger.info("Message processing loop started")
        while self._running or not self._message_queue.empty():
            try:
                msg = await asyncio.wait_for(self._message_queue.get(), timeout=1.0)
                logger.info(
                    "Queue recv: sender=%s channel=%s ts=%s thread_ts=%s content=%.80s",
                    msg.get("sender", "?"), msg.get("channel_name", "?"),
                    msg.get("ts", "?"), msg.get("thread_ts", "None"),
                    msg.get("content", "")[:80],
                )
                try:
                    await self._handle_channel_message(msg)
                except Exception:
                    logger.exception("Error handling message from %s in %s",
                                     msg.get("sender", "?"), msg.get("channel_name", "?"))
                self._message_queue.task_done()
            except asyncio.TimeoutError:
                if not self.is_within_time_limit:
                    logger.info("Time limit reached, waiting for queue to drain...")
                    if self._message_queue.empty():
                        break
        logger.info("Message processing loop exited (_running=%s, queue_empty=%s)",
                     self._running, self._message_queue.empty())

    def _update_thread_meta(self, msg: dict) -> None:
        """Track thread participants and OP reply status for thread discipline."""
        thread_ts = msg.get("thread_ts")
        msg_ts = msg.get("ts")
        sender_name = msg.get("sender", "")
        sender_id = self._infer_sender_agent(sender_name) or sender_name.lower()

        if not thread_ts:
            # Top-level message — register as potential thread OP
            if msg_ts:
                self._thread_meta[msg_ts] = {
                    "op": sender_id,
                    "participants": {sender_id},
                    "op_replied": False,
                    "reply_count": 0,
                }
            return

        # Thread reply — update the thread's metadata
        meta = self._thread_meta.get(thread_ts)
        if not meta:
            # Thread we don't have metadata for (started before simulation)
            self._thread_meta[thread_ts] = {
                "op": None,
                "participants": {sender_id},
                "op_replied": True,  # assume mature if we missed the start
                "reply_count": 1,
            }
            return

        meta["participants"].add(sender_id)
        meta["reply_count"] += 1
        if sender_id == meta["op"]:
            meta["op_replied"] = True

    def _is_thread_open_for(self, msg: dict, agent_id: str) -> bool:
        """Check whether thread discipline allows this agent to respond.

        Hard gates:
        - Top-level messages with no thread yet: anyone can respond
        - Existing participants always allowed
        - Hard cap at 2 participants per thread (OP + one responder)
        """
        thread_ts = msg.get("thread_ts")
        if not thread_ts:
            # Top-level message — but check if it has acquired a thread
            # (an earlier agent in the sequential loop may have replied)
            msg_ts = msg.get("ts")
            if msg_ts:
                meta = self._thread_meta.get(msg_ts)
                if meta and meta["reply_count"] > 0:
                    # This top-level message now has a thread; apply thread gates
                    logger.info(
                        "Thread discipline: top-level msg ts=%s now has thread (%d replies), applying gates",
                        msg_ts, meta["reply_count"],
                    )
                    thread_ts = msg_ts
                else:
                    return True  # genuinely no thread yet
            else:
                return True

        meta = self._thread_meta.get(thread_ts)
        if not meta:
            return True  # no tracking data, allow

        # Always allow the OP and existing participants
        if agent_id in meta["participants"]:
            return True

        # Hard cap: 2 participants max per thread (OP + one responder)
        if len(meta["participants"]) >= 2:
            return False

        return True

    async def _handle_channel_message(self, msg: dict) -> None:
        """Process a single incoming Slack message across all relevant agents."""
        # Deduplicate: each Slack message arrives N times (once per bot in the channel).
        # Only process the first copy.
        msg_ts = msg.get("ts")
        if msg_ts:
            if msg_ts in self._seen_message_ts:
                return
            self._seen_message_ts.add(msg_ts)

        channel_name = msg.get("channel_name", "unknown")
        sender_agent_id = msg.get("agent_id")  # Which agent received this event

        # Update channel history
        if channel_name not in self._channel_history:
            self._channel_history[channel_name] = []
        self._channel_history[channel_name].append({
            "sender": msg.get("sender", "unknown"),
            "content": msg.get("content", ""),
            "ts": msg.get("ts"),
        })

        # Track thread participants for thread discipline
        self._update_thread_meta(msg)

        # Don't respond if time limit or budget exceeded
        if not self.is_within_time_limit:
            return

        # Determine which agents should evaluate this message
        # (all agents in the channel, excluding the sender)
        sender_name = msg.get("sender", "")
        responding_agents = []
        skip_reasons: dict[str, str] = {}

        for agent in self.agents.values():
            # Skip if this agent sent the message
            if agent.bot_name.lower() in sender_name.lower():
                skip_reasons[agent.agent_id] = "is_sender"
                continue
            # Skip if agent isn't in this channel
            if channel_name not in self._agent_channels.get(agent.agent_id, set()):
                skip_reasons[agent.agent_id] = f"not_in_channel({channel_name})"
                continue
            if not self._agent_within_budget(agent):
                skip_reasons[agent.agent_id] = "over_budget"
                continue
            # Thread discipline: don't even let agents decide if the thread is too young
            if not self._is_thread_open_for(msg, agent.agent_id):
                skip_reasons[agent.agent_id] = "thread_discipline"
                continue
            responding_agents.append(agent)

        if not responding_agents:
            logger.info(
                "No responding agents for msg in #%s from '%s'. Skips: %s",
                channel_name, sender_name, skip_reasons,
            )
            return

        logger.info(
            "%d agents evaluating msg in #%s from '%s': %s",
            len(responding_agents), channel_name, sender_name,
            [a.agent_id for a in responding_agents],
        )

        # Randomize decision order so no agent consistently evaluates first
        random.shuffle(responding_agents)

        # Sequential decide-then-respond: each agent decides one at a time so
        # later agents see earlier replies before making their decision.  The
        # agent's decide prompt receives thread metadata (participant count,
        # reply count, whether OP has replied) so it can judge whether to join
        # the thread, wait, or start a new top-level message.

        for agent in responding_agents:
            delay = random.uniform(5, 30)
            await asyncio.sleep(delay)

            # Re-check budget and time
            if not self.is_within_time_limit or not self._agent_within_budget(agent):
                continue

            # Re-check thread discipline (may have changed since earlier agent responded)
            if not self._is_thread_open_for(msg, agent.agent_id):
                logger.debug(
                    "[%s] Skipped — thread discipline (after earlier responses)",
                    agent.agent_id,
                )
                continue

            # Decide (with current channel history that includes earlier replies)
            try:
                decision = await self._agent_decide(agent, channel_name, msg)
            except Exception as exc:
                logger.warning("[%s] Decision error: %s", agent.agent_id, exc)
                continue

            if not decision.get("should_respond"):
                continue

            # Get updated channel history (includes any replies posted since we started)
            history = self._channel_history.get(channel_name, [])

            try:
                action = decision.get("action", "respond")
                if action == "respond":
                    response_model = self._select_response_model(decision)
                    response_text = await agent.respond(
                        channel_name=channel_name,
                        channel_history=history[:-1],
                        new_message=msg,
                        action_context=decision.get("reason", ""),
                        model=response_model,
                    )
                    # Reply in thread — use the original message's thread_ts if it's
                    # already in a thread, otherwise use its ts to start a new thread.
                    reply_thread_ts = msg.get("thread_ts") or msg.get("ts")
                    await self._post_message(agent.agent_id, channel_name, response_text, thread_ts=reply_thread_ts)

                    # Update thread meta with this new participant
                    if reply_thread_ts:
                        meta = self._thread_meta.get(reply_thread_ts)
                        if meta:
                            meta["participants"].add(agent.agent_id)
                            meta["reply_count"] += 1

                elif action == "new_thread":
                    # Agent wants to start a new top-level message referencing the original
                    response_model = self._select_response_model(decision)
                    # Provide context so the agent knows to reference the original post
                    original_sender = msg.get("sender", "someone")
                    action_context = (
                        f"You are starting a NEW top-level message in #{channel_name}, inspired by "
                        f"{original_sender}'s post. Reference their post but take a different angle. "
                        f"Original context: {decision.get('reason', '')}"
                    )
                    response_text = await agent.respond(
                        channel_name=channel_name,
                        channel_history=history,
                        new_message=msg,
                        action_context=action_context,
                        model=response_model,
                    )
                    # Post as top-level (no thread_ts)
                    if self._can_post_toplevel(agent.agent_id, channel_name):
                        await self._post_message(agent.agent_id, channel_name, response_text)

            except Exception as exc:
                logger.error("[%s] Response failed: %s", agent.agent_id, exc)

        # Flush any buffered LLM logs after processing all responses
        if self._llm_log_buffer:
            await self._flush_llm_logs()

    def _build_thread_context(self, msg: dict) -> str:
        """Build a human-readable summary of thread state for the decide prompt."""
        thread_ts = msg.get("thread_ts")
        if not thread_ts:
            # This is a top-level message, but it may have acquired replies since
            # it was posted (e.g., an earlier agent in the sequential loop replied).
            # Check if this message's ts has become a thread root.
            msg_ts = msg.get("ts")
            if msg_ts:
                meta = self._thread_meta.get(msg_ts)
                if meta and meta["reply_count"] > 0:
                    logger.info(
                        "Thread context: top-level msg ts=%s now has %d replies, %d participants",
                        msg_ts, meta["reply_count"], len(meta["participants"]),
                    )
                    participants = sorted(meta["participants"])
                    parts = [
                        f"This post already has a thread with {len(participants)} participants: {', '.join(participants)}.",
                        f"Replies so far: {meta['reply_count']}.",
                        f"RULE: Threads are limited to 2 participants. If you are not already a participant, "
                        f"you MUST NOT join this thread. Use action \"new_thread\" to start a separate "
                        f"top-level message referencing this post instead.",
                    ]
                    return "\n".join(parts)
            return "This is a top-level message (no thread yet). You may respond to start a 1-on-1 discussion."

        meta = self._thread_meta.get(thread_ts)
        if not meta:
            return "This is a thread reply (no metadata available)."

        participants = sorted(meta["participants"])
        parts = [
            f"Thread participants: {', '.join(participants)} ({len(participants)} total).",
            f"Replies so far: {meta['reply_count']}.",
        ]
        if len(participants) >= 2:
            parts.append(
                "RULE: This thread already has 2 participants. No new participants may join. "
                "If you are not one of the participants listed above, use action \"new_thread\"."
            )
        if meta["reply_count"] >= 4:
            parts.append(
                "This thread has had several exchanges. You should be working toward a conclusion: "
                "either a :memo: Summary with a strong collaboration proposal, or a graceful close "
                "acknowledging insufficient overlap."
            )
        return "\n".join(parts)

    async def _agent_decide(self, agent: Agent, channel_name: str, msg: dict) -> dict:
        """Run Phase 1 decision for an agent. Always uses Sonnet."""
        settings = get_settings()
        history = self._channel_history.get(channel_name, [])
        thread_context = self._build_thread_context(msg)
        return await agent.decide(
            channel_name=channel_name,
            channel_history=history[:-1],
            new_message=msg,
            thread_context=thread_context,
            model=settings.llm_agent_model_sonnet,
        )

    def _can_post_toplevel(self, agent_id: str, channel: str) -> bool:
        """Check if this agent can still post a new top-level message in this channel."""
        key = (agent_id, channel)
        return self._toplevel_posts.get(key, 0) < self.max_toplevel_per_channel

    async def _post_message(self, agent_id: str, channel: str, text: str, thread_ts: str | None = None) -> None:
        """Post a message and record it in the database."""
        # Track top-level posts
        if not thread_ts:
            key = (agent_id, channel)
            self._toplevel_posts[key] = self._toplevel_posts.get(key, 0) + 1

        client = self.slack_clients.get(agent_id)
        agent = self.agents.get(agent_id)

        result = None
        if client:
            result = client.post_message(channel, text, thread_ts=thread_ts)
        else:
            logger.info("[%s] MOCK post to #%s: %s...", agent_id, channel, text[:60])

        # Update channel history
        if channel not in self._channel_history:
            self._channel_history[channel] = []
        bot_name = agent.bot_name if agent else f"{agent_id}Bot"
        self._channel_history[channel].append({
            "sender": bot_name,
            "content": text,
            "ts": result.get("ts") if result else None,
        })

        # Log to database
        if self.session_factory and self.simulation_run_id:
            await self._log_message(
                agent_id=agent_id,
                channel_id=result.get("channel", channel) if result else channel,
                channel_name=channel,
                message_ts=result.get("ts") if result else None,
                message_length=len(text),
                phase="respond",
            )

    async def _log_message(
        self,
        agent_id: str,
        channel_id: str,
        channel_name: str,
        message_ts: str | None,
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
                    agent = self.agents.get(agent_id)
                    if agent:
                        run.total_api_calls = sum(
                            a.api_call_count for a in self.agents.values()
                        )
                await db.commit()
        except Exception as exc:
            logger.warning("Failed to log message: %s", exc)

    # Channels every bot joins regardless of profile
    _UNIVERSAL_CHANNELS = {"general", "funding-opportunities"}

    # Keywords that indicate relevance to each topic channel
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

    def _pick_channels_for_agent(self, agent: Agent) -> list[str]:
        """Decide which seeded channels an agent should join based on its profile."""
        profile_text = agent.public_profile.lower()
        channels = list(self._UNIVERSAL_CHANNELS)
        for channel_name, keywords in self._CHANNEL_KEYWORDS.items():
            if any(kw in profile_text for kw in keywords):
                channels.append(channel_name)
        return channels

    def _ensure_seeded_channels(self) -> None:
        """Create any missing seeded channels and join relevant bots."""
        # Use the first available client to create channels
        creator = next(iter(self.slack_clients.values()), None)
        if not creator or not creator._app:
            return

        try:
            result = creator._app.client.conversations_list(types="public_channel", limit=200)
            existing = {ch["name"]: ch["id"] for ch in result.get("channels", [])}
        except Exception as exc:
            logger.warning("Failed to list channels: %s", exc)
            return

        # Ensure all seeded channels exist
        for channel_name in SEEDED_CHANNELS:
            if channel_name not in existing:
                logger.info("Creating seeded channel #%s", channel_name)
                channel_data = creator.create_channel(channel_name)
                if channel_data:
                    existing[channel_name] = channel_data.get("id", "")

        # Update channel name→ID map for all clients (includes newly created channels)
        for client in self.slack_clients.values():
            client._channel_name_to_id.update(existing)

        # Join each bot to its relevant channels
        for agent_id, client in self.slack_clients.items():
            agent = self.agents.get(agent_id)
            if not agent:
                continue
            channels_to_join = self._pick_channels_for_agent(agent)
            self._agent_channels[agent_id] = set(channels_to_join)
            for ch_name in channels_to_join:
                ch_id = existing.get(ch_name)
                if ch_id:
                    client.join_channel(ch_id)
            logger.info("[%s] Joined channels: %s", agent_id, ", ".join(sorted(channels_to_join)))

    def _infer_sender_agent(self, sender_name: str) -> str | None:
        """Try to infer which agent sent a message from their bot name."""
        sender_lower = sender_name.lower()
        for agent_id in self.agents:
            if agent_id in sender_lower:
                return agent_id
        return None

    async def update_all_working_memories(self) -> None:
        """Update working memory for all agents after the simulation."""
        for agent in self.agents.values():
            if not self.session_factory or not self.simulation_run_id:
                continue
            try:
                from sqlalchemy import select
                async with self.session_factory() as db:
                    result = await db.execute(
                        select(AgentMessage).where(
                            AgentMessage.simulation_run_id == self.simulation_run_id,
                            AgentMessage.agent_id == agent.agent_id,
                        )
                    )
                    messages = result.scalars().all()
                    recent = [
                        {"channel": m.channel_name, "content": ""}
                        for m in messages
                    ]
                if recent:
                    await agent.update_working_memory(recent)
            except Exception as exc:
                logger.error("[%s] Working memory update failed: %s", agent.agent_id, exc)


def _default_kickstart_messages() -> list[dict]:
    """Default scripted kickstart messages."""
    return [
        {
            "agent": "su",
            "channel": "general",
            "message": (
                "Hi everyone — the Su lab just published a new paper on using BioThings Explorer "
                "for systematic drug repurposing in rare diseases. We identified several promising "
                "candidates for Niemann-Pick disease type C using our knowledge graph approach. "
                "Would love to discuss with anyone working on rare disease models or compound screening."
            ),
        },
        {
            "agent": "cravatt",
            "channel": "chemical-biology",
            "message": (
                "We've been mapping the covalent ligandable proteome and have new data on "
                "compound-protein interactions at protein-protein interfaces. Our ABPP platform "
                "has identified hundreds of new cysteine-reactive sites that appear druggable. "
                "Curious if anyone here is working on structural characterization of these binding "
                "sites or has computational approaches for predicting druggability at PPI interfaces."
            ),
        },
        {
            "agent": "lotz",
            "channel": "single-cell-omics",
            "message": (
                "Our lab has generated several large single-cell RNA-seq datasets from "
                "osteoarthritic and healthy cartilage tissue, as well as intervertebral disc "
                "samples across multiple time points. We're looking for computational collaborators "
                "to help with integration and meta-analysis across datasets — particularly anyone "
                "with experience aligning datasets from different donors and conditions."
            ),
        },
    ]
