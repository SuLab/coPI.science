"""Agent class — holds identity, profiles, and generates responses."""

import logging
import os
from pathlib import Path
from typing import Any

from src.services.llm import generate_agent_response, make_decision

logger = logging.getLogger(__name__)

PROFILES_DIR = Path("profiles")
PROMPTS_DIR = Path("prompts")


class Agent:
    """
    Represents a single lab agent (Slack bot).
    Holds identity, profiles, and orchestrates LLM calls.
    """

    def __init__(self, agent_id: str, bot_name: str, pi_name: str):
        self.agent_id = agent_id  # e.g., "su"
        self.bot_name = bot_name  # e.g., "SuBot"
        self.pi_name = pi_name  # e.g., "Andrew Su"
        self._public_profile: str | None = None
        self._private_profile: str | None = None
        self._base_system_prompt: str | None = None
        self._decision_prompt: str | None = None
        self._lab_directory: str | None = None  # Shared across agents
        self.api_call_count: int = 0
        self.message_count: int = 0

    @property
    def public_profile(self) -> str:
        if self._public_profile is None:
            self._public_profile = self._load_file(
                PROFILES_DIR / "public" / f"{self.agent_id}.md",
                f"# {self.pi_name} Lab\n\nProfile not yet available.",
            )
        return self._public_profile

    @property
    def private_profile(self) -> str:
        if self._private_profile is None:
            self._private_profile = self._load_file(
                PROFILES_DIR / "private" / f"{self.agent_id}.md",
                "No private instructions yet.",
            )
        return self._private_profile

    @property
    def base_system_prompt(self) -> str:
        if self._base_system_prompt is None:
            self._base_system_prompt = self._load_file(
                PROMPTS_DIR / "agent-system.md",
                _default_system_prompt(),
            )
        return self._base_system_prompt

    @property
    def decision_prompt_template(self) -> str:
        if self._decision_prompt is None:
            self._decision_prompt = self._load_file(
                PROMPTS_DIR / "agent-respond-decision.md",
                _default_decision_prompt(),
            )
        return self._decision_prompt

    def reload_profiles(self):
        """Reload profiles from disk (call after working memory updates)."""
        self._public_profile = None
        self._private_profile = None

    def build_system_prompt(self, channel_name: str, channel_description: str = "") -> str:
        """Build the full system prompt for an agent in a given channel."""
        lab_directory_section = ""
        if self._lab_directory:
            lab_directory_section = f"""
## Other Labs' Recent Publications
Use these to reference other labs' work in conversations. Include links when citing.
{self._lab_directory}
"""
        return f"""{self.base_system_prompt}

## Your Identity
You are **{self.bot_name}**, the AI agent representing the {self.pi_name} lab at Scripps Research.
Your agent ID is "{self.agent_id}". When communicating, represent your lab professionally.

## Your Lab Profile (Public)
{self.public_profile}

## Your Private Instructions
{self.private_profile}
{lab_directory_section}
## Current Context
Channel: #{channel_name}
{f"Channel description: {channel_description}" if channel_description else ""}
"""

    async def decide(
        self,
        channel_name: str,
        channel_history: list[dict],
        new_message: dict,
        model: str | None = None,
    ) -> dict:
        """
        Phase 1: Decide whether to respond to a message.
        Returns {should_respond, action, reason}.
        """
        system_prompt = self.build_system_prompt(channel_name)
        decision_system = f"{system_prompt}\n\n{self.decision_prompt_template}"

        history_text = _format_history(channel_history)
        new_msg_text = _format_message(new_message)

        messages = [
            {
                "role": "user",
                "content": f"""You just received this message in #{channel_name}:

{new_msg_text}

Recent channel history:
{history_text}

Decide whether and how to respond. Output ONLY valid JSON.""",
            }
        ]

        self.api_call_count += 1
        try:
            result = await make_decision(
                decision_system,
                messages,
                model=model,
                log_meta={"agent_id": self.agent_id, "phase": "decide"},
            )
            return result
        except Exception as exc:
            logger.error("[%s] Decision call failed: %s", self.agent_id, exc)
            return {"should_respond": False, "action": "ignore", "reason": str(exc)}

    async def respond(
        self,
        channel_name: str,
        channel_history: list[dict],
        new_message: dict,
        action_context: str = "",
        model: str | None = None,
    ) -> str:
        """
        Phase 2: Generate a response to post in Slack.
        """
        system_prompt = self.build_system_prompt(channel_name)

        history_text = _format_history(channel_history)
        new_msg_text = _format_message(new_message)

        messages = [
            {
                "role": "user",
                "content": f"""You received this message in #{channel_name}:

{new_msg_text}

Recent channel history:
{history_text}

{f"Context: {action_context}" if action_context else ""}

Respond naturally as {self.bot_name}. Be specific, concrete, and substantive.
Do not use markdown headers — just write naturally as in a Slack message.
Keep your response focused and under 500 words.""",
            }
        ]

        self.api_call_count += 1
        self.message_count += 1
        try:
            response = await generate_agent_response(
                system_prompt=system_prompt,
                messages=messages,
                model=model,
                max_tokens=800,
                log_meta={"agent_id": self.agent_id, "phase": "respond"},
            )
            return response
        except Exception as exc:
            logger.error("[%s] Response call failed: %s", self.agent_id, exc)
            raise

    async def generate_kickstart_message(
        self,
        channel_name: str,
        model: str | None = None,
    ) -> str:
        """Generate a kickstart message to initiate conversation."""
        system_prompt = self.build_system_prompt(channel_name)

        messages = [
            {
                "role": "user",
                "content": f"""You've just joined the #{channel_name} channel.
Introduce a recent result, finding, or open question from your lab that would genuinely interest
other researchers here. Be specific — mention specific experiments, datasets, or techniques.
Don't just say what your lab "does" generally; share something concrete and current.
Keep it to 2-4 sentences. Don't use markdown headers.""",
            }
        ]

        self.api_call_count += 1
        self.message_count += 1
        response = await generate_agent_response(
            system_prompt=system_prompt,
            messages=messages,
            model=model,
            max_tokens=400,
            log_meta={"agent_id": self.agent_id, "phase": "kickstart"},
        )
        return response

    async def update_working_memory(
        self,
        recent_messages: list[dict],
        model: str | None = None,
    ) -> str:
        """Update working memory after a simulation run."""
        system_prompt = self.build_system_prompt("memory-update")

        messages_text = "\n".join(
            f"[{m.get('channel', 'unknown')}] {m.get('content', '')[:200]}"
            for m in recent_messages[:30]
        )

        messages = [
            {
                "role": "user",
                "content": f"""Based on your recent conversations in the Slack workspace, update your working memory.

Your recent messages:
{messages_text}

Write an updated working memory section that summarizes:
(a) Collaboration opportunities you've identified and their status
(b) Feedback or directions from your PI (if any)
(c) Your current understanding of priorities

Keep it concise — this is a living summary, not a log. Under 300 words.""",
            }
        ]

        self.api_call_count += 1
        response = await generate_agent_response(
            system_prompt=system_prompt,
            messages=messages,
            model=model,
            max_tokens=400,
            log_meta={"agent_id": self.agent_id, "phase": "memory"},
        )

        # Write updated working memory to private profile
        self._update_working_memory_file(response)
        return response

    def _update_working_memory_file(self, new_memory: str) -> None:
        """Update the working memory section in the private profile file."""
        private_path = PROFILES_DIR / "private" / f"{self.agent_id}.md"
        current = self._load_file(private_path, "")

        marker = "## Working Memory"
        if marker in current:
            # Replace everything from marker onwards
            idx = current.index(marker)
            new_content = current[:idx] + f"{marker}\n\n{new_memory}\n"
        else:
            new_content = current + f"\n\n{marker}\n\n{new_memory}\n"

        try:
            private_path.parent.mkdir(parents=True, exist_ok=True)
            private_path.write_text(new_content, encoding="utf-8")
            self._private_profile = None  # Invalidate cache
        except Exception as exc:
            logger.error("[%s] Failed to update working memory: %s", self.agent_id, exc)

    @staticmethod
    def _load_file(path: Path, default: str) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return default


def _format_history(messages: list[dict]) -> str:
    """Format channel history for LLM context."""
    if not messages:
        return "(no previous messages)"
    parts = []
    for msg in messages[-20:]:  # Last 20 messages
        sender = msg.get("sender", "unknown")
        content = msg.get("content", "")
        parts.append(f"{sender}: {content}")
    return "\n".join(parts)


def _format_message(message: dict) -> str:
    """Format a single message for LLM context."""
    sender = message.get("sender", "unknown")
    content = message.get("content", "")
    return f"{sender}: {content}"


def _default_system_prompt() -> str:
    return """You are an AI agent representing a research lab at Scripps Research in a Slack workspace
called "labbot". Your role is to facilitate scientific collaboration by engaging with other lab agents.

## Core Principles

1. **Specificity over generality.** Every collaboration idea must name specific techniques, models,
   reagents, datasets, or expertise. Generic contributions ("computational analysis", "structural studies")
   without specific scientific context are not acceptable.

2. **True complementarity.** Each lab must bring something the other doesn't have.
   If either lab's contribution could be done by hiring a postdoc independently, the collaboration
   idea is too generic.

3. **Concrete first experiment required.** Any collaboration beyond initial interest must include
   a proposed first experiment scoped to days-to-weeks, naming specific assays, methods, or reagents.

4. **Silence is better than noise.** If you can't articulate what makes this collaboration better
   than either lab doing it alone, don't propose it.

5. **Non-generic benefits.** Both labs must benefit in ways specific to the collaboration.

## Confidence Labels
- **High:** Clear complementarity, concrete first experiment, both sides benefit non-generically
- **Moderate:** Good synergy but first experiment less defined, or one benefit less clear
- **Speculative:** Interesting but needs development — label these ("This is speculative, but...")

## Communication Style
- Professional but not stiff — like a knowledgeable postdoc representing the lab
- Specific and concrete, not vague
- Willing to say "I don't know, let me check with my PI"
- Doesn't oversell or overcommit
- Expresses genuine enthusiasm when there's real synergy

## Rules
- Cannot commit effort or resources on behalf of your PI
- Cannot share private profile information
- Cannot DM other labs' PIs (only DM your own PI)
- When a promising collaboration is identified, explore it to a concrete first experiment
  before naturally pausing for human review"""


def _default_decision_prompt() -> str:
    return """Evaluate whether you should respond to this message in Slack.

Consider:
- Is the message directly relevant to your lab's expertise?
- Are you directly addressed or tagged?
- Is there a genuine collaboration opportunity worth exploring?
- Do you have something specific and substantive to add?

Do NOT respond if:
- You're just being polite
- Another agent already said what you would say
- You have nothing specific to contribute
- The topic is completely outside your lab's expertise

Return ONLY this JSON (no other text):
{
  "should_respond": true or false,
  "action": "respond" | "ignore" | "create_channel" | "dm_pi",
  "reason": "brief reason for your decision"
}"""
