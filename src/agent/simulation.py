"""Simulation engine — coordinates all agents across all channels."""

import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Any

from src.agent.agent import Agent
from src.agent.channels import SEEDED_CHANNELS, make_collaboration_channel_name
from src.models import AgentMessage, SimulationRun

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

        # Register message handlers
        for agent_id, client in self.slack_clients.items():
            if hasattr(client, "on_message"):
                original_on_message = client.on_message
                # Wrap to push into async queue
                def make_handler(aid):
                    def handler(msg):
                        asyncio.run_coroutine_threadsafe(
                            self._message_queue.put(msg),
                            asyncio.get_event_loop(),
                        )
                    return handler
                client.on_message = make_handler(agent_id)

        # Run kickstart and message processing concurrently
        await asyncio.gather(
            self._run_kickstart(),
            self._process_messages(),
        )

    async def stop(self) -> None:
        """Stop the simulation gracefully."""
        self._running = False
        logger.info("Simulation stopping...")

    async def _run_kickstart(self) -> None:
        """Post scripted and generated kickstart messages to initiate conversation."""
        kickstart_config = self._load_kickstart_config()

        # Post scripted openers first
        for opener in kickstart_config.get("scripted", []):
            agent_id = opener.get("agent")
            channel = opener.get("channel", "general").lstrip("#")
            message = opener.get("message", "")

            if not message or agent_id not in self.agents:
                continue

            await asyncio.sleep(random.uniform(5, 30))
            await self._post_message(agent_id, channel, message)

        # Generated openers for remaining agents
        scripted_agents = {o.get("agent") for o in kickstart_config.get("scripted", [])}
        remaining_agents = [a for a in self.agents.values() if a.agent_id not in scripted_agents]

        for agent in remaining_agents:
            if not self.is_within_time_limit:
                break
            await asyncio.sleep(random.uniform(10, 60))
            try:
                channel = random.choice(["general", "drug-repurposing", "structural-biology",
                                        "aging-and-longevity", "chemical-biology"])
                message = await agent.generate_kickstart_message(channel)
                await self._post_message(agent.agent_id, channel, message)
            except Exception as exc:
                logger.error("[%s] Kickstart failed: %s", agent.agent_id, exc)

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
        while self._running or not self._message_queue.empty():
            try:
                msg = await asyncio.wait_for(self._message_queue.get(), timeout=1.0)
                await self._handle_channel_message(msg)
                self._message_queue.task_done()
            except asyncio.TimeoutError:
                if not self.is_within_time_limit:
                    logger.info("Time limit reached, waiting for queue to drain...")
                    if self._message_queue.empty():
                        break

    async def _handle_channel_message(self, msg: dict) -> None:
        """Process a single incoming Slack message across all relevant agents."""
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

        # Don't respond if time limit or budget exceeded
        if not self.is_within_time_limit:
            return

        # Determine which agents should evaluate this message
        # (all agents in the channel, excluding the sender)
        sender_name = msg.get("sender", "")
        responding_agents = []

        for agent in self.agents.values():
            # Skip if this agent sent the message
            if agent.bot_name.lower() in sender_name.lower():
                continue
            if not self._agent_within_budget(agent):
                continue
            responding_agents.append(agent)

        if not responding_agents:
            return

        # Phase 1: All agents decide in parallel
        decisions = await asyncio.gather(
            *[
                self._agent_decide(agent, channel_name, msg)
                for agent in responding_agents
            ],
            return_exceptions=True,
        )

        # Collect agents that want to respond
        responders = []
        for agent, decision in zip(responding_agents, decisions):
            if isinstance(decision, Exception):
                logger.warning("[%s] Decision error: %s", agent.agent_id, decision)
                continue
            if decision.get("should_respond"):
                responders.append((agent, decision))

        if not responders:
            return

        # Phase 2: Stagger responses
        shuffle_responders = list(responders)
        random.shuffle(shuffle_responders)

        for agent, decision in shuffle_responders:
            delay = random.uniform(5, 30)
            await asyncio.sleep(delay)

            # Re-check budget and time
            if not self.is_within_time_limit or not self._agent_within_budget(agent):
                continue

            # Get updated channel history
            history = self._channel_history.get(channel_name, [])

            try:
                action = decision.get("action", "respond")
                if action == "respond":
                    response_text = await agent.respond(
                        channel_name=channel_name,
                        channel_history=history[:-1],
                        new_message=msg,
                        action_context=decision.get("reason", ""),
                    )
                    await self._post_message(agent.agent_id, channel_name, response_text)

                elif action == "create_channel":
                    # Create a collaboration channel
                    sender_agent = self._infer_sender_agent(sender_name)
                    if sender_agent:
                        new_channel_name = make_collaboration_channel_name(
                            [agent.agent_id, sender_agent],
                            topic=decision.get("reason", "collab")[:20],
                        )
                        await self._create_and_notify_channel(
                            agent.agent_id, new_channel_name
                        )

            except Exception as exc:
                logger.error("[%s] Response failed: %s", agent.agent_id, exc)

    async def _agent_decide(self, agent: Agent, channel_name: str, msg: dict) -> dict:
        """Run Phase 1 decision for an agent."""
        history = self._channel_history.get(channel_name, [])
        return await agent.decide(
            channel_name=channel_name,
            channel_history=history[:-1],
            new_message=msg,
        )

    async def _post_message(self, agent_id: str, channel: str, text: str) -> None:
        """Post a message and record it in the database."""
        client = self.slack_clients.get(agent_id)
        agent = self.agents.get(agent_id)

        result = None
        if client:
            result = client.post_message(channel, text)
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

    async def _create_and_notify_channel(
        self, creator_agent_id: str, channel_name: str
    ) -> None:
        """Create a collaboration channel and notify relevant parties."""
        client = self.slack_clients.get(creator_agent_id)
        if client:
            channel_data = client.create_channel(channel_name)
            if channel_data:
                channel_id = channel_data.get("id", channel_name)
                # Join all relevant agents
                for agent_id, ac in self.slack_clients.items():
                    ac.join_channel(channel_id)
                logger.info(
                    "[%s] Created collaboration channel #%s", creator_agent_id, channel_name
                )

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
