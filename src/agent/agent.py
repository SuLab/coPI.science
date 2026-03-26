"""Agent class — holds identity, profiles, and builds prompts for each phase."""

import logging
import re
from pathlib import Path
from typing import Any

from src.agent.state import AgentState, PostRef, ThreadState

logger = logging.getLogger(__name__)

PROFILES_DIR = Path("profiles")
PROMPTS_DIR = Path("prompts")


class Agent:
    """
    Represents a single lab agent (Slack bot).
    Holds identity, profiles, and per-simulation mutable state.
    """

    def __init__(self, agent_id: str, bot_name: str, pi_name: str):
        self.agent_id = agent_id  # e.g., "su"
        self.bot_name = bot_name  # e.g., "SuBot"
        self.pi_name = pi_name  # e.g., "Andrew Su"
        self._public_profile: str | None = None
        self._private_profile: str | None = None
        self._working_memory: str | None = None
        self._lab_directory: str | None = None
        self.api_call_count: int = 0
        self.message_count: int = 0
        self.state = AgentState()

    # ------------------------------------------------------------------
    # Profile properties (cached, loaded from disk)
    # ------------------------------------------------------------------

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
    def working_memory(self) -> str:
        if self._working_memory is None:
            self._working_memory = self._load_file(
                PROFILES_DIR / "memory" / f"{self.agent_id}.md",
                "",
            )
        return self._working_memory

    def reload_profiles(self):
        """Reload profiles from disk."""
        self._public_profile = None
        self._private_profile = None
        self._working_memory = None

    # ------------------------------------------------------------------
    # System prompt (shared across all phases)
    # ------------------------------------------------------------------

    def build_system_prompt(self) -> str:
        """Build the full agent system prompt with identity and profiles."""
        base_prompt = self._load_file(
            PROMPTS_DIR / "agent-system.md",
            _default_system_prompt(),
        )
        lab_directory_section = ""
        if self._lab_directory:
            lab_directory_section = f"""
## Other Labs' Recent Publications
Use these to reference other labs' work in conversations. Include links when citing.
{self._lab_directory}
"""
        return f"""{base_prompt}

## Your Identity
You are **{self.bot_name}**, the AI agent representing the {self.pi_name} lab at Scripps Research.
Your agent ID is "{self.agent_id}". When communicating, represent your lab professionally.

## Your Lab Profile (Public)
{self.public_profile}

## Your Private Instructions
{self.private_profile}

## Your Working Memory
{self.working_memory if self.working_memory else "*No working memory yet — this is your first simulation.*"}
{lab_directory_section}"""

    # ------------------------------------------------------------------
    # Phase 2: Scan & Filter prompt
    # ------------------------------------------------------------------

    def build_phase2_scan_prompt(self, new_posts: list[dict[str, str]]) -> tuple[str, list[dict]]:
        """
        Build system + messages for Phase 2 scan/filter.

        new_posts: list of {post_id, channel, sender, content_snippet}
        Returns (system_prompt, messages).
        """
        system_prompt = self.build_system_prompt()
        phase2_template = self._load_file(
            PROMPTS_DIR / "phase2-scan-filter.md",
            "Evaluate posts and return JSON with selected_post_ids.",
        )

        # Format posts for the prompt
        posts_text = "\n\n".join(
            f"**Post ID: {p['post_id']}** in #{p['channel']} by {p['sender']}:\n{p['content_snippet']}"
            for p in new_posts
        )
        prompt = phase2_template.replace("{new_posts}", posts_text)

        messages = [{"role": "user", "content": prompt}]
        return system_prompt, messages

    def build_phase2_prune_prompt(self) -> tuple[str, list[dict]]:
        """Build system + messages for Phase 2 prune."""
        system_prompt = self.build_system_prompt()
        prune_template = self._load_file(
            PROMPTS_DIR / "phase2-prune.md",
            "Prune interesting_posts to ≤20. Return JSON with keep_post_ids.",
        )

        posts_text = "\n\n".join(
            f"**Post ID: {p.post_id}** in #{p.channel} by {p.sender_agent_id}:\n{p.content_snippet}"
            for p in self.state.interesting_posts
        )
        prompt = prune_template.replace("{interesting_posts}", posts_text)

        messages = [{"role": "user", "content": prompt}]
        return system_prompt, messages

    # ------------------------------------------------------------------
    # Phase 4: Thread Reply prompt
    # ------------------------------------------------------------------

    def build_phase4_prompt(
        self,
        thread: ThreadState,
        thread_history: list[dict[str, str]],
        other_agent_name: str,
        other_agent_lab: str,
    ) -> tuple[str, list[dict]]:
        """
        Build system + messages for Phase 4 thread reply.

        thread_history: list of {sender, content} dicts.
        Returns (system_prompt, messages).
        """
        system_prompt = self.build_system_prompt()
        phase4_template = self._load_file(
            PROMPTS_DIR / "phase4-thread-reply.md",
            "Compose a thread reply.",
        )

        # Thread phase guidance
        if thread.message_count <= 4:
            thread_phase = "EXPLORE"
            phase_guidance = (
                "You are in the EXPLORE phase. Share relevant specifics from your lab's recent work. "
                "Ask clarifying questions about the other lab's capabilities. Use retrieve_profile and "
                "retrieve_abstract tools to learn more. Do NOT propose a full collaboration yet."
            )
        elif thread.message_count <= 11:
            thread_phase = "DECIDE"
            phase_guidance = (
                "You are in the DECIDE phase. Narrow the scope: is there genuine complementarity? "
                "Can you name a specific first experiment? If yes, build toward a :memo: Summary proposal. "
                "If no, begin wrapping up gracefully."
            )
        else:
            thread_phase = "MUST CONCLUDE"
            phase_guidance = (
                "This is message 12 — you MUST conclude the thread now. Either post a :memo: Summary "
                "with a collaboration proposal, or close gracefully acknowledging insufficient overlap."
            )

        # Format thread history
        history_text = "\n".join(
            f"**{m['sender']}**: {m['content']}" for m in thread_history
        )

        # Build instructions based on phase
        if thread_phase == "EXPLORE":
            instructions = (
                "Write a reply that shares specific details from your lab and asks a clarifying "
                "question. Use tools proactively to research the other lab."
            )
        elif thread_phase == "DECIDE":
            instructions = (
                "Write a reply that moves toward a conclusion. Either build toward a specific "
                ":memo: Summary proposal or acknowledge insufficient overlap."
            )
        else:
            instructions = (
                "This is the final message. You MUST either:\n"
                "1. Post a :memo: Summary with a specific collaboration proposal, OR\n"
                "2. If the other agent already posted a :memo: Summary you agree with, reply with ✅, OR\n"
                "3. Close gracefully explaining why there's no good proposal."
            )

        prompt_text = phase4_template.replace("{channel_name}", thread.channel)
        prompt_text = prompt_text.replace("{other_agent_name}", other_agent_name)
        prompt_text = prompt_text.replace("{other_agent_lab}", other_agent_lab)
        prompt_text = prompt_text.replace("{message_count}", str(thread.message_count))
        prompt_text = prompt_text.replace("{thread_phase}", thread_phase)
        prompt_text = prompt_text.replace("{thread_history}", history_text)
        prompt_text = prompt_text.replace("{phase_guidance}", phase_guidance)
        prompt_text = prompt_text.replace("{instructions}", instructions)

        messages = [{"role": "user", "content": prompt_text}]
        return system_prompt, messages

    # ------------------------------------------------------------------
    # Phase 5: New Post prompt
    # ------------------------------------------------------------------

    def build_phase5_prompt(self) -> tuple[str, list[dict]]:
        """
        Build system + messages for Phase 5 new post.
        Returns (system_prompt, messages).
        """
        system_prompt = self.build_system_prompt()
        phase5_template = self._load_file(
            PROMPTS_DIR / "phase5-new-post.md",
            "Choose to reply to an interesting post or make a new top-level post.",
        )

        # Format interesting posts
        if self.state.interesting_posts:
            interesting_text = "\n\n".join(
                f"**Post ID: {p.post_id}** in #{p.channel} by {p.sender_agent_id}:\n{p.content_snippet}"
                for p in self.state.interesting_posts
            )
        else:
            interesting_text = "(none)"

        # Format subscribed channels
        channels_text = ", ".join(f"#{ch}" for ch in sorted(self.state.subscribed_channels))

        prompt_text = phase5_template.replace("{interesting_posts}", interesting_text)
        prompt_text = prompt_text.replace("{subscribed_channels}", channels_text)

        messages = [{"role": "user", "content": prompt_text}]
        return system_prompt, messages

    # ------------------------------------------------------------------
    # Working memory update
    # ------------------------------------------------------------------

    def update_working_memory_file(self, new_memory: str) -> None:
        """Write working memory to profiles/memory/{agent_id}.md."""
        memory_path = PROFILES_DIR / "memory" / f"{self.agent_id}.md"
        try:
            memory_path.parent.mkdir(parents=True, exist_ok=True)
            memory_path.write_text(new_memory + "\n", encoding="utf-8")
            self._working_memory = None  # Invalidate cache
        except Exception as exc:
            logger.error("[%s] Failed to update working memory: %s", self.agent_id, exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_file(path: Path, default: str) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return default


def _default_system_prompt() -> str:
    return """You are an AI agent representing a research lab at Scripps Research in a Slack workspace
called "labbot". Your role is to facilitate scientific collaboration by engaging with other lab agents.

## Core Principles

1. **Specificity over generality.** Every collaboration idea must name specific techniques, models,
   reagents, datasets, or expertise. Generic contributions ("computational analysis", "structural studies")
   without specific scientific context are not acceptable.

2. **True complementarity.** Each lab must bring something the other doesn't have.

3. **Concrete first experiment required.** Any collaboration beyond initial interest must include
   a proposed first experiment scoped to days-to-weeks, naming specific assays, methods, or reagents.

4. **Silence is better than noise.** If you can't articulate what makes this collaboration better
   than either lab doing it alone, don't propose it.

5. **Non-generic benefits.** Both labs must benefit in ways specific to the collaboration.

## Communication Style
- Professional but not stiff — like a knowledgeable postdoc representing the lab
- Specific and concrete, not vague
- Willing to say "I don't know, let me check with my PI"
- Doesn't oversell or overcommit
- Expresses genuine enthusiasm when there's real synergy

## Rules
- Cannot commit effort or resources on behalf of your PI
- Cannot share private profile information
- Cannot DM other labs' PIs (only DM your own PI)"""
