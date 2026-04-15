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

    def build_scan_system_prompt(self) -> str:
        """Build a lightweight system prompt for scan/filter phases.

        Omits working memory and lab directory — scan only needs identity,
        research focus, and private priorities to judge relevance.
        """
        base_prompt = self._load_file(
            PROMPTS_DIR / "agent-system.md",
            _default_system_prompt(),
        )
        return f"""{base_prompt}

## Your Identity
You are **{self.bot_name}**, the AI agent representing the {self.pi_name} lab at Scripps Research.
Your agent ID is "{self.agent_id}". When communicating, represent your lab professionally.

## Your Lab Profile (Public)
{self.public_profile}

## Your Private Instructions
{self.private_profile}"""

    def build_thread_reply_system_prompt(self) -> str:
        """Build a system prompt for thread replies.

        Omits lab directory — by mid-conversation you already know who you're
        talking to. Use retrieve_profile tool if you need details on another lab.
        Includes working memory since it may contain thread-relevant context.
        """
        base_prompt = self._load_file(
            PROMPTS_DIR / "agent-system.md",
            _default_system_prompt(),
        )
        return f"""{base_prompt}

## Your Identity
You are **{self.bot_name}**, the AI agent representing the {self.pi_name} lab at Scripps Research.
Your agent ID is "{self.agent_id}". When communicating, represent your lab professionally.

## Your Lab Profile (Public)
{self.public_profile}

## Your Private Instructions
{self.private_profile}

## Your Working Memory
{self.working_memory if self.working_memory else "*No working memory yet — this is your first simulation.*"}"""

    # ------------------------------------------------------------------
    # Phase 2: Scan & Filter prompt
    # ------------------------------------------------------------------

    def build_phase2_scan_prompt(self, new_posts: list[dict[str, str]]) -> tuple[str, list[dict]]:
        """
        Build system + messages for Phase 2 scan/filter.

        new_posts: list of {post_id, channel, sender, content_snippet}
        Returns (system_prompt, messages).
        """
        system_prompt = self.build_scan_system_prompt()
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
        system_prompt = self.build_scan_system_prompt()
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
        is_funding_thread: bool = False,
        your_prior_messages: str | None = None,
        thread_activity_summary: str | None = None,
    ) -> tuple[str, list[dict]]:
        """
        Build system + messages for Phase 4 thread reply.

        thread_history: list of {sender, content} dicts.
        Returns (system_prompt, messages).
        """
        system_prompt = self.build_thread_reply_system_prompt()
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
                "If no, start your reply with ⏸️ and explain graciously why there's no viable collaboration. "
                "It is OK to conclude with no proposal — not every conversation leads to one."
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
                "2. If the other agent already posted a :memo: Summary you agree with AS-IS, reply with ✅ "
                "(no modifications — if you want changes, post your own revised :memo: Summary instead), OR\n"
                "3. Start your reply with ⏸️ and close gracefully explaining why there's no good proposal.\n\n"
                "Option 3 is perfectly acceptable — not every conversation should end in a proposal."
            )

        # Inject PI context if the PI posted in this thread
        if thread.pi_context:
            phase_guidance += (
                f"\n\n**Your PI has posted in this thread.** Their message is authoritative — "
                f"incorporate their direction into your reply. If they corrected something you "
                f"said, acknowledge the correction to the other agent. PI's message: "
                f"\"{thread.pi_context}\""
            )

        # Funding-thread context block: rendered only when this is a :moneybag: thread.
        if is_funding_thread:
            funding_ctx_lines = [
                "## Funding thread — additional rules",
                "",
                "This is a :moneybag: funding thread. In addition to the normal reply rules:",
                "",
                "- **No announcement-only replies.** Do not post replies that merely announce "
                "a future spin-off ('I'll start a new thread', 'watch for my post', "
                "'posting it now', 'thread wrapped'). Either create the spin-off post this "
                "turn via a new top-level :moneybag: post, or reply only with substantive "
                "content (a new aim, a specific contribution, a scoping question).",
                "- **No acknowledgment-only replies.** 'Sounds good', 'thanks', 'see you "
                "there', 'agreed' are not allowed. Every reply must add substantive content.",
                "- **Self-dedup.** If you have already replied in this thread, your next "
                "reply must build on the discussion — do not repost the same alignment "
                "pitch. See your prior messages below.",
                "",
                "### Your prior messages in this thread",
                "",
                your_prior_messages or "(none — this would be your first reply)",
                "",
                "### Prior activity in this thread",
                "",
                thread_activity_summary or "(no prior activity)",
                "",
            ]
            funding_context = "\n".join(funding_ctx_lines)
        else:
            funding_context = ""

        prompt_text = phase4_template.replace("{channel_name}", thread.channel)
        prompt_text = prompt_text.replace("{other_agent_name}", other_agent_name)
        prompt_text = prompt_text.replace("{other_agent_lab}", other_agent_lab)
        prompt_text = prompt_text.replace("{message_count}", str(thread.message_count))
        prompt_text = prompt_text.replace("{thread_phase}", thread_phase)
        prompt_text = prompt_text.replace("{thread_history}", history_text)
        prompt_text = prompt_text.replace("{phase_guidance}", phase_guidance)
        prompt_text = prompt_text.replace("{instructions}", instructions)
        prompt_text = prompt_text.replace("{foa_number}", thread.foa_number or "none")
        prompt_text = prompt_text.replace("{funding_thread_context}", funding_context)

        messages = [{"role": "user", "content": prompt_text}]
        return system_prompt, messages

    # ------------------------------------------------------------------
    # Phase 5: New Post prompt
    # ------------------------------------------------------------------

    def build_phase5_prompt(
        self,
        recent_posts: list[dict[str, str]] | None = None,
        foa_contexts: dict[str, str] | None = None,
        thread_foa_contexts: dict[str, str] | None = None,
        prior_threads: dict[str, list[dict]] | None = None,
        funding_only: bool = False,
        funding_thread_summaries: dict[str, str] | None = None,
    ) -> tuple[str, list[dict]]:
        """
        Build system + messages for Phase 5 new post.
        recent_posts: [{channel, content_snippet}] — agent's own recent top-level posts.
        foa_contexts: {post_id: formatted_foa_text} — pre-loaded FOA details for funding posts.
        thread_foa_contexts: {foa_number: formatted_foa_text} — FOAs from active threads
            available for Option B (starting a funding collaboration).
        prior_threads: {other_agent_id: [{channel, outcome, summary}]} — all closed threads
            grouped by other agent, for dedup context.
        funding_only: if True, strip prompt to funding actions only (agent is blocked for
            regular posts but has funding posts available).
        Returns (system_prompt, messages).
        """
        system_prompt = self.build_system_prompt()
        phase5_template = self._load_file(
            PROMPTS_DIR / "phase5-new-post.md",
            "Choose to reply to an interesting post or make a new top-level post.",
        )

        # Format interesting posts, injecting FOA details for funding posts
        if self.state.interesting_posts:
            parts = []
            for p in self.state.interesting_posts:
                part = f"**Post ID: {p.post_id}** in #{p.channel} by {p.sender_agent_id}:\n{p.content_snippet}"
                if foa_contexts and p.post_id in foa_contexts:
                    part += f"\n\n<foa_details foa_number=\"{p.foa_number}\">\n{foa_contexts[p.post_id]}\n</foa_details>"
                if funding_thread_summaries and p.post_id in funding_thread_summaries:
                    part += (
                        f"\n\n<thread_activity post_id=\"{p.post_id}\">\n"
                        f"{funding_thread_summaries[p.post_id]}\n"
                        f"</thread_activity>"
                    )
                parts.append(part)
            interesting_text = "\n\n".join(parts)
        else:
            interesting_text = "(none)"

        # Format subscribed channels
        channels_text = ", ".join(f"#{ch}" for ch in sorted(self.state.subscribed_channels))

        # Format recent posts by this agent
        if recent_posts:
            recent_text = "\n\n".join(
                f"- #{p['channel']}: {p['content_snippet']}"
                for p in recent_posts
            )
        else:
            recent_text = "(none)"

        # Format prior conversations for dedup
        if prior_threads:
            prior_parts = []
            for other_id in sorted(prior_threads):
                agent_label = f"{other_id.capitalize()}Bot"
                thread_lines = []
                for t in prior_threads[other_id]:
                    outcome_label = t["outcome"].replace("_", " ")
                    if t.get("summary"):
                        thread_lines.append(
                            f"- #{t['channel']} — {outcome_label}: {t['summary']}"
                        )
                    else:
                        thread_lines.append(
                            f"- #{t['channel']} — {outcome_label}"
                        )
                prior_parts.append(f"**{agent_label}**\n" + "\n".join(thread_lines))
            prior_text = "\n\n".join(prior_parts)
        else:
            prior_text = "(none)"

        if funding_only:
            # Strip prompt to funding-only actions: reply to funding posts,
            # start a funding collab, or skip. Remove sections that would
            # tempt the LLM into proposing regular posts that will be rejected.
            import re
            phase5_template = re.sub(
                r"## Your subscribed channels\n.*?\n\{subscribed_channels\}\n",
                "",
                phase5_template,
                flags=re.DOTALL,
            )
            phase5_template = re.sub(
                r"## Your recent posts\n.*?\{your_recent_posts\}\n",
                "",
                phase5_template,
                flags=re.DOTALL,
            )
            phase5_template = re.sub(
                r"## Prior conversations with other labs\n.*?\{prior_conversations\}\n",
                "",
                phase5_template,
                flags=re.DOTALL,
            )
            phase5_template = re.sub(
                r"### Option C: Make a new top-level post\n.*?(?=### Option D:)",
                "",
                phase5_template,
                flags=re.DOTALL,
            )
            # Replace intro text to clarify the constraint
            phase5_template = phase5_template.replace(
                "You have the opportunity to either reply to an interesting post or make a new top-level\n"
                "post in one of your subscribed channels.",
                "You have unreviewed proposals, so you can only take funding-related actions this turn.\n"
                "Reply to a funding post, start a funding collaboration, or skip.",
            )

        prompt_text = phase5_template.replace("{interesting_posts}", interesting_text)
        prompt_text = prompt_text.replace("{subscribed_channels}", channels_text)
        prompt_text = prompt_text.replace("{your_recent_posts}", recent_text)
        prompt_text = prompt_text.replace("{prior_conversations}", prior_text)

        # Inject pre-loaded FOA details for Option B (funding collaborations)
        if thread_foa_contexts:
            foa_section = "\n\n## Available FOA details for funding collaborations\n\n"
            foa_section += "\n\n".join(
                f"<foa_details foa_number=\"{foa_num}\">\n{foa_text}\n</foa_details>"
                for foa_num, foa_text in thread_foa_contexts.items()
            )
            prompt_text += foa_section

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

    def update_private_profile(self, new_profile: str) -> None:
        """Write private profile to profiles/private/{agent_id}.md (disk only).

        For DB persistence, call persist_private_profile_to_db() afterward.
        """
        profile_path = PROFILES_DIR / "private" / f"{self.agent_id}.md"
        try:
            profile_path.parent.mkdir(parents=True, exist_ok=True)
            profile_path.write_text(new_profile + "\n", encoding="utf-8")
            self._private_profile = None  # Invalidate cache
        except Exception as exc:
            logger.error("[%s] Failed to update private profile: %s", self.agent_id, exc)

    async def persist_private_profile_to_db(self, db: "AsyncSession") -> None:
        """Sync the on-disk private profile to the database."""
        from sqlalchemy import select
        from src.models import AgentRegistry, ResearcherProfile

        try:
            agent_result = await db.execute(
                select(AgentRegistry).where(AgentRegistry.agent_id == self.agent_id)
            )
            agent_reg = agent_result.scalar_one_or_none()
            if not agent_reg:
                return
            profile_result = await db.execute(
                select(ResearcherProfile).where(
                    ResearcherProfile.user_id == agent_reg.user_id
                )
            )
            profile = profile_result.scalar_one_or_none()
            if profile:
                profile.private_profile_md = self.private_profile
                await db.commit()
        except Exception as exc:
            logger.error("[%s] Failed to persist private profile to DB: %s", self.agent_id, exc)

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
