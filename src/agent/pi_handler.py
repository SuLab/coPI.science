"""PI interaction handler — processes DMs, tags, and thread interventions."""

import json
import logging
import re
from pathlib import Path
from typing import Any

from src.agent.agent import Agent
from src.agent.message_log import LogEntry, MessageLog
from src.agent.state import PostRef
from src.config import get_settings
from src.services.llm import generate_agent_response

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path("prompts")


class PIHandler:
    """Handles all PI-to-bot interactions."""

    def __init__(
        self,
        agents: dict[str, Agent],
        slack_clients: dict,  # agent_id -> AgentSlackClient
        pi_slack_id_to_agent_id: dict[str, str],
        message_log: MessageLog,
        session_factory=None,
    ):
        self.agents = agents
        self.slack_clients = slack_clients
        self.pi_slack_id_to_agent_id = pi_slack_id_to_agent_id
        # Reverse mapping: agent_id -> PI slack_user_id
        self.agent_id_to_pi_slack_id = {v: k for k, v in pi_slack_id_to_agent_id.items()}
        self.message_log = message_log
        self.session_factory = session_factory

    # ------------------------------------------------------------------
    # DM handling
    # ------------------------------------------------------------------

    async def handle_dm(self, agent_id: str, pi_slack_id: str, text: str) -> None:
        """Process a DM from a PI to their bot."""
        classification = await self._classify_dm(text)
        category = classification.get("category", "question")

        if category == "standing_instruction":
            await self._handle_standing_instruction(agent_id, pi_slack_id, text)
        elif category == "feedback":
            if classification.get("implies_standing_instruction"):
                await self._handle_standing_instruction(agent_id, pi_slack_id, text)
            else:
                await self._send_dm(agent_id, pi_slack_id,
                    "Thanks for the feedback — I'll keep that in mind for future interactions.")
        elif category == "question":
            await self._send_dm(agent_id, pi_slack_id,
                "I'll be able to answer questions like this soon. "
                "For now, you can check my activity on the admin dashboard at copi.science.")
        else:
            logger.warning("[%s] Unknown DM category: %s", agent_id, category)

    async def _classify_dm(self, text: str) -> dict[str, Any]:
        """Classify a PI DM into category using LLM."""
        prompt_template = PROMPTS_DIR / "pi-dm-classify.md"
        system_prompt = prompt_template.read_text(encoding="utf-8").replace("{pi_message}", text)

        try:
            settings = get_settings()
            response = await generate_agent_response(
                system_prompt=system_prompt,
                messages=[{"role": "user", "content": text}],
                model=settings.llm_agent_model_sonnet,
                max_tokens=200,
                log_meta={"agent_id": "pi_handler", "phase": "dm_classify"},
            )
            return self._parse_json(response)
        except Exception as exc:
            logger.warning("DM classification failed: %s", exc)
            return {"category": "question", "implies_standing_instruction": False}

    async def _handle_standing_instruction(
        self, agent_id: str, pi_slack_id: str, instruction: str,
    ) -> None:
        """Rewrite private profile with new PI instruction and notify."""
        agent = self.agents.get(agent_id)
        if not agent:
            return

        current_profile = agent.private_profile
        prompt_template = PROMPTS_DIR / "pi-profile-rewrite.md"
        system_prompt = (
            prompt_template.read_text(encoding="utf-8")
            .replace("{current_profile}", current_profile)
            .replace("{pi_instruction}", instruction)
        )

        try:
            settings = get_settings()
            response = await generate_agent_response(
                system_prompt=system_prompt,
                messages=[{"role": "user", "content": f"Incorporate this instruction: {instruction}"}],
                model=settings.llm_agent_model_sonnet,
                max_tokens=2000,
                log_meta={"agent_id": agent_id, "phase": "profile_rewrite"},
            )

            # Parse profile and changes from response
            profile_match = re.search(r"<profile>(.*?)</profile>", response, re.DOTALL)
            changes_match = re.search(r"<changes>(.*?)</changes>", response, re.DOTALL)

            if profile_match:
                new_profile = profile_match.group(1).strip()
                agent.update_private_profile(new_profile)
                changes = changes_match.group(1).strip() if changes_match else "Profile updated."

                confirmation = (
                    f"I've updated my private profile to reflect your instruction. "
                    f"Here's what changed: {changes}\n\n"
                    f"Let me know if anything looks off, or you can edit the profile "
                    f"directly at copi.science."
                )
                await self._send_dm(agent_id, pi_slack_id, confirmation)
                logger.info("[%s] Private profile rewritten per PI instruction", agent_id)
            else:
                logger.warning("[%s] Profile rewrite response missing <profile> tags", agent_id)
                await self._send_dm(agent_id, pi_slack_id,
                    "I received your instruction but had trouble updating my profile. "
                    "You can edit it directly at copi.science.")
        except Exception as exc:
            logger.error("[%s] Profile rewrite failed: %s", agent_id, exc, exc_info=True)
            await self._send_dm(agent_id, pi_slack_id,
                "I received your instruction but encountered an error updating my profile. "
                "You can edit it directly at copi.science.")

    # ------------------------------------------------------------------
    # Channel tag handling
    # ------------------------------------------------------------------

    async def handle_channel_tag(self, agent_id: str, entry: LogEntry) -> None:
        """Handle a PI tagging their bot in a channel post."""
        agent = self.agents.get(agent_id)
        if not agent:
            return

        target_post_id = entry.thread_ts or entry.ts
        pi_text = entry.content

        # Check if thread already has 2 agent participants
        allowed = self.message_log.get_thread_allowed_agents(target_post_id)
        if allowed and len(allowed) >= 2 and agent_id not in allowed:
            # Can't join — find most relevant agent and start new thread
            other_agents = list(allowed)
            dm_text = (
                f"That thread already has two agents ({', '.join(other_agents)}). "
                f"I'll start a new conversation referencing it."
            )
            # Add as PI-priority with context for creating a new post
            agent.state.interesting_posts.append(PostRef(
                post_id=target_post_id,
                channel=entry.channel,
                sender_agent_id=other_agents[0] if other_agents else "unknown",
                content_snippet=pi_text[:200],
                posted_at=entry.posted_at,
                pi_priority=True,
                pi_context=f"PI said: {pi_text}. Note: original thread has 2 agents, start a new thread with the most relevant one.",
            ))
        else:
            # Can join — add as PI-priority
            agent.state.interesting_posts.append(PostRef(
                post_id=target_post_id,
                channel=entry.channel,
                sender_agent_id=entry.sender_agent_id or entry.sender_name,
                content_snippet=entry.content[:200],
                posted_at=entry.posted_at,
                pi_priority=True,
                pi_context=f"PI said: {pi_text}",
            ))
            dm_text = f"Saw your tag on a post in #{entry.channel}. I'll engage in that thread."

        # Confirm via DM
        pi_slack_id = self.agent_id_to_pi_slack_id.get(agent_id)
        if pi_slack_id:
            await self._send_dm(agent_id, pi_slack_id, dm_text)

        logger.info("[%s] PI tag processed in #%s", agent_id, entry.channel)

    # ------------------------------------------------------------------
    # Thread conclusion notifications
    # ------------------------------------------------------------------

    async def notify_thread_conclusion(
        self,
        agent_id: str,
        thread: Any,  # ThreadState
        outcome: str,
        summary_text: str | None = None,
    ) -> None:
        """DM the PI when a thread reaches a conclusion."""
        pi_slack_id = self.agent_id_to_pi_slack_id.get(agent_id)
        if not pi_slack_id:
            return

        other_bot = self.agents.get(thread.other_agent_id)
        other_name = other_bot.bot_name if other_bot else thread.other_agent_id
        channel = thread.channel

        if outcome == "proposal":
            brief = summary_text[:200] + "..." if summary_text and len(summary_text) > 200 else (summary_text or "")
            text = (
                f"I just posted a collaboration proposal with {other_name} in #{channel}.\n\n"
                f"_{brief}_\n\n"
                f"You can review this proposal at copi.science."
            )
        elif outcome == "no_proposal":
            text = (
                f"Closed the thread with {other_name} in #{channel} — "
                f"didn't find a strong enough collaboration angle to propose."
            )
        elif outcome == "timeout":
            text = (
                f"Thread with {other_name} in #{channel} timed out "
                f"(reached message limit without a conclusion)."
            )
        else:
            return

        await self._send_dm(agent_id, pi_slack_id, text)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _send_dm(self, agent_id: str, pi_slack_id: str, text: str) -> None:
        """Send a DM from the agent's bot to the PI."""
        client = self.slack_clients.get(agent_id)
        if client and client.is_connected:
            client.send_dm(pi_slack_id, text)
        else:
            logger.debug("[%s] Cannot send DM — no connected client", agent_id)

    @staticmethod
    def _parse_json(text: str) -> dict:
        """Extract JSON from an LLM response."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if "```" in cleaned:
                cleaned = cleaned[:cleaned.index("```")]
            cleaned = cleaned.strip()
        start = cleaned.find("{")
        if start >= 0:
            depth = 0
            for i, ch in enumerate(cleaned[start:], start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        cleaned = cleaned[start:i + 1]
                        break
        return json.loads(cleaned)
