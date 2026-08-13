"""Agent class — holds identity, profiles, and builds prompts for each phase."""

import logging
import re
import time
from pathlib import Path

from src.agent.post_types import render_menu
from src.agent.prompt_safety import delimit
from src.agent.roles import DEFAULT_ROLE, load_role, resolve_prompt_path
from src.agent.state import AgentState, ThreadState
from src.agent.thread_guidance import phase4_guidance
from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE, VISIBILITY_PUBLIC

logger = logging.getLogger(__name__)

PROFILES_DIR = Path("profiles")

# Matches a bare DOI. The character class deliberately excludes the delimiters
# that wrap DOIs in Slack posts (whitespace, quotes, angle brackets from
# <https://doi.org/...> links, and the ) ] that close markdown/parentheticals).
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>)\]]+", re.IGNORECASE)


def _extract_dois(text: str | None) -> set[str]:
    """Return the set of normalized DOIs found in ``text`` (lowercased)."""
    out: set[str] = set()
    for raw in _DOI_RE.findall(text or ""):
        out.add(raw.rstrip(".,;").lower())
    return out


class Agent:
    """
    Represents a single lab agent (Slack bot).
    Holds identity, profiles, and per-simulation mutable state.
    """

    def __init__(self, agent_id: str, bot_name: str, pi_name: str,
                 role: str = DEFAULT_ROLE):
        self.agent_id = agent_id  # e.g., "su"
        self.bot_name = bot_name  # e.g., "SuBot"
        self.pi_name = pi_name  # e.g., "Andrew Su"
        self.role = role  # e.g., "pi_lab" — selects prompt/role overrides
        self._public_profile: str | None = None
        self._public_working_memory: str | None = None  # cached public memory segment
        self._own_publication_dois: set[str] | None = None  # cached DOIs from own profiles
        self._lab_directory: str | None = None
        self.api_call_count: int = 0
        self.message_count: int = 0
        self.state = AgentState()
        # Cohort interaction gate: set of agent_ids this agent may act on (its
        # cohort-mates), or None when isolation is disabled (all-vs-all).
        # Recomputed each roster sync by SimulationEngine. See specs/cohort-system.md.
        self.allowed_sender_ids: set[str] | None = None

    def record_api_call(self, now: float | None = None) -> None:
        """Record one LLM call against both the lifetime counter and the
        sliding-window ledger.

        The single write point for both. Every call site must use this rather
        than bumping ``api_call_count`` directly — a site that bumps only the
        counter is invisible to the rate limiter, and a site that appends only to
        the ledger corrupts ``SimulationRun.total_api_calls``.
        """
        self.api_call_count += 1
        self.state.call_times.append(time.time() if now is None else now)

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
    def public_working_memory(self) -> str:
        """Working memory derived from public channels only.

        Path: profiles/memory/{agent_id}/public.md. Falls back to the legacy
        profiles/memory/{agent_id}.md path when the partitioned layout hasn't
        been created yet — safe because all legacy content derives from public
        channels (private channels didn't exist pre-partition).

        See specs/privacy-and-channel-visibility.md §G2.
        """
        if self._public_working_memory is None:
            new_path = PROFILES_DIR / "memory" / self.agent_id / "public.md"
            legacy_path = PROFILES_DIR / "memory" / f"{self.agent_id}.md"
            if new_path.exists():
                self._public_working_memory = self._load_file(new_path, "")
            else:
                self._public_working_memory = self._load_file(legacy_path, "")
        return self._public_working_memory

    def get_private_channel_memory(self, channel_id: str) -> str:
        """Working memory scoped to a single collab_private channel.

        Returns empty string if no memory has been synthesized yet for that
        channel. Not cached — files are small and read only when the agent
        acts in the channel.
        """
        path = PROFILES_DIR / "memory" / self.agent_id / "private" / f"{channel_id}.md"
        return self._load_file(path, "")

    # Back-compat alias: internal callers that don't yet thread a visibility
    # argument still work and always see the public segment (safe default —
    # never private content). Prefer public_working_memory in new code.
    @property
    def working_memory(self) -> str:
        return self.public_working_memory

    @property
    def own_publication_dois(self) -> set[str]:
        """DOIs of the lab's own papers, parsed from its public profile.

        Used to detect when a post or thread is about a paper this lab
        (co)authored. The public profile lists each PI's representative
        publications with DOIs, so a DOI appearing here means the paper is the
        lab's own work. Note this only catches papers whose DOI is present in
        the profile — a prose-only profile yields an empty set, which is why
        the scan/reply prompts also instruct the model to recognize its own
        published methods semantically. See GitHub issue #7.

        Derives from the public profile only — there is no private-profile
        segment to union anymore (private instructions were removed).
        """
        if self._own_publication_dois is None:
            self._own_publication_dois = _extract_dois(self.public_profile)
        return self._own_publication_dois

    def cites_own_paper(self, content: str | None) -> bool:
        """True if ``content`` cites a DOI belonging to this lab's own papers."""
        own = self.own_publication_dois
        if not own:
            return False
        return bool(_extract_dois(content) & own)

    def reload_profiles(self):
        """Reload profiles from disk."""
        self._public_profile = None
        self._public_working_memory = None
        self._own_publication_dois = None

    # ------------------------------------------------------------------
    # System prompt (shared across all phases)
    # ------------------------------------------------------------------

    def build_system_prompt(
        self,
        visibility: str = VISIBILITY_PUBLIC,
        channel_id: str | None = None,
    ) -> str:
        """Build the full agent system prompt with identity and profiles.

        visibility: the visibility class of the channel the agent is about to
        act in. When 'collab_private', the private-channel memory segment for
        ``channel_id`` is also injected. See specs/privacy-and-channel-visibility.md §G1.
        """
        return self._compose_system_prompt(
            include_memory=True,
            include_lab_directory=True,
            visibility=visibility,
            channel_id=channel_id,
        )

    def build_scan_system_prompt(self) -> str:
        """Build a lightweight system prompt for scan/filter phases.

        Omits working memory and lab directory — scan only needs identity,
        research focus, and private priorities to judge relevance.
        """
        return self._compose_system_prompt(
            include_memory=False,
            include_lab_directory=False,
        )

    def build_thread_reply_system_prompt(
        self,
        visibility: str = VISIBILITY_PUBLIC,
        channel_id: str | None = None,
    ) -> str:
        """Build a system prompt for thread replies.

        Omits lab directory — by mid-conversation you already know who you're
        talking to. Use retrieve_profile tool if you need details on another lab.
        Includes working memory since it may contain thread-relevant context.

        visibility/channel_id: same semantics as build_system_prompt — determines
        which memory segment is injected.
        """
        return self._compose_system_prompt(
            include_memory=True,
            include_lab_directory=False,
            visibility=visibility,
            channel_id=channel_id,
        )

    def _load_prompt(self, filename: str, default: str) -> str:
        """Load a prompt file honouring this agent's role override.

        See src/agent/roles.py: ``pi_lab`` (the default role) always falls
        through to the global ``prompts/{filename}`` — that fallthrough *is*
        what keeps existing agents byte-identical after this method's
        introduction.
        """
        return self._load_file(resolve_prompt_path(self.role, filename), default)

    def _render_identity(self) -> str:
        """Render the '## Your Identity' block for this agent."""
        template = self._load_prompt("identity.md", _DEFAULT_IDENTITY)
        # str.replace, NOT str.format: profiles/role files may contain bare
        # curly braces (e.g. "budget is {tight}") that must not be treated as
        # format fields.
        return (
            template.replace("{bot_name}", self.bot_name)
            .replace("{pi_name}", self.pi_name)
            .replace("{agent_id}", self.agent_id)
        )

    def _compose_system_prompt(
        self,
        *,
        include_memory: bool,
        include_lab_directory: bool,
        visibility: str = VISIBILITY_PUBLIC,
        channel_id: str | None = None,
    ) -> str:
        """Assemble a system prompt from the shared sections.

        This is the single composer behind build_system_prompt,
        build_scan_system_prompt, and build_thread_reply_system_prompt — the
        include_memory/include_lab_directory flags reproduce each builder's
        original section set byte-for-byte (see the callers below).
        """
        base_prompt = self._load_prompt("agent-system.md", _default_system_prompt())
        identity = self._render_identity()

        header = f"""{base_prompt}

{identity}

## Your Lab Profile (Public)
{self.public_profile}"""

        if not include_memory:
            return header

        working_memory_text = self._compose_working_memory(visibility, channel_id)
        memory_block = f"\n\n## Your Working Memory\n{working_memory_text}"

        if include_lab_directory:
            lab_directory_section = ""
            if self._lab_directory:
                lab_directory_section = f"""
## Other Labs' Recent Publications
Use these to reference other labs' work in conversations. Include links when citing.
{self._lab_directory}
"""
            return f"{header}{memory_block}\n{lab_directory_section}"

        return f"{header}{memory_block}"

    def _compose_working_memory(
        self,
        visibility: str,
        channel_id: str | None,
    ) -> str:
        """Compose the working-memory section of a system prompt.

        Public-only for public/collab_public actions; public + the specific
        private-channel segment for collab_private actions. See
        specs/privacy-and-channel-visibility.md §G1, §G2.
        """
        segments: list[str] = []
        public_segment = self.public_working_memory
        if public_segment:
            segments.append(public_segment)
        if visibility == VISIBILITY_COLLAB_PRIVATE and channel_id:
            private_segment = self.get_private_channel_memory(channel_id)
            if private_segment:
                segments.append(
                    f"### Private channel notes (scope: this channel only)\n{private_segment}"
                )
        if not segments:
            return "*No working memory yet — this is your first simulation.*"
        return "\n\n".join(segments)

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
        phase2_template = self._load_prompt(
            "phase2-scan-filter.md",
            "Evaluate posts and return JSON with selected_post_ids.",
        )

        # Format posts for the prompt.
        post_blocks: list[str] = []
        for p in new_posts:
            header = f"**Post ID: {p['post_id']}** in #{p['channel']} by {p['sender']}:"
            # Post bodies come from other labs' agents — fence as untrusted
            # peer content so an injected instruction can't hijack the scan
            # decision (SEC-14).
            post_blocks.append(f"{header}\n{delimit(p['content_snippet'], 'post_content')}")
        posts_text = "\n\n".join(post_blocks)
        prompt = phase2_template.replace("{new_posts}", posts_text)

        messages = [{"role": "user", "content": prompt}]
        return system_prompt, messages

    def build_phase2_prune_prompt(self) -> tuple[str, list[dict]]:
        """Build system + messages for Phase 2 prune."""
        system_prompt = self.build_scan_system_prompt()
        prune_template = self._load_prompt(
            "phase2-prune.md",
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
        visibility: str = VISIBILITY_PUBLIC,
        channel_id: str | None = None,
    ) -> tuple[str, list[dict]]:
        """
        Build system + messages for Phase 4 thread reply.

        thread_history: list of {sender, content} dicts.
        visibility/channel_id: visibility class of the thread's channel and the
            channel's Slack ID. Threaded through to the system prompt builder
            for visibility-scoped memory injection.
        Returns (system_prompt, messages).
        """
        system_prompt = self.build_thread_reply_system_prompt(
            visibility=visibility, channel_id=channel_id,
        )
        phase4_template = self._load_prompt(
            "phase4-thread-reply.md",
            "Compose a thread reply.",
        )

        # Thread phase guidance + instructions, per role. scout_hub scouts ideas
        # against Blackbird's screening rubric; it has no lab and never proposes a
        # collaboration. See src/agent/thread_guidance.py.
        thread_phase, phase_guidance, instructions = phase4_guidance(
            self.role, thread.message_count
        )

        # Format thread history
        history_text = "\n".join(
            f"**{m['sender']}**: {m['content']}" for m in thread_history
        )

        # If the thread's root post is about a paper this lab authored, warn the
        # model not to engage as if it were external work (see issue #7).
        root_content = thread_history[0]["content"] if thread_history else ""
        if self.cites_own_paper(root_content):
            phase_guidance += (
                "\n\n**⚠️ This thread's root post cites a paper your own lab authored.** "
                "Speak as its author — do not describe it as external work — and focus on "
                "what remains unexploited beyond the published scope."
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

    def build_phase5_prompt(
        self,
        recent_posts: list[dict[str, str]] | None = None,
        prior_threads: dict[str, list[dict]] | None = None,
        visibility: str = VISIBILITY_PUBLIC,
        channel_id: str | None = None,
        post_type_menu: str | None = None,
    ) -> tuple[str, list[dict]]:
        """
        Build system + messages for Phase 5 new post.
        recent_posts: [{channel, content_snippet}] — agent's own recent top-level posts.
        prior_threads: {other_agent_id: [{channel, outcome, summary}]} — all closed threads
            grouped by other agent, for dedup context.
        Returns (system_prompt, messages).

        visibility/channel_id: Phase 5 is the "new post" phase, which in v1
            always operates in a public channel. The parameters are plumbed
            through for symmetry with the other phase builders; future work
            that lets agents initiate private-channel posts will use them.

        post_type_menu: pre-rendered {post_type_menu} block. The engine computes
            it from the role's allow-list filtered by the live cohort gate, and
            enforces the SAME set when the response comes back. None renders
            THIS AGENT'S ROLE's declared set with no topology filtering — used by
            direct callers and tests that have no topology to apply.
        """
        system_prompt = self.build_system_prompt(visibility=visibility, channel_id=channel_id)
        phase5_template = self._load_prompt(
            "phase5-new-post.md",
            "Choose to reply to an interesting post or make a new top-level post.",
        )

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

        prompt_text = phase5_template.replace("{subscribed_channels}", channels_text)
        prompt_text = prompt_text.replace("{your_recent_posts}", recent_text)
        prompt_text = prompt_text.replace("{prior_conversations}", prior_text)
        if post_type_menu is None:
            # No topology supplied — render THIS agent's role set with no
            # filtering, matching the "gate is None means no filtering" rule.
            # Role-aware, not DEFAULT_POST_TYPES: a scout_hub agent built by a
            # direct caller would otherwise get the pi_lab menu, offering it
            # three types its own role.toml forbids.
            #
            # gate=None also makes render_menu emit guidance instead of an
            # enumeration for an addressed type. There is no roster here to
            # enumerate, and the enumeration would come out as the literal
            # "one of: ." — in a live prompt, and in test_phase5_prompt_gm's
            # committed snapshot.
            post_type_menu = render_menu(
                load_role(self.role).post_types, gate=None, roles_by_agent={},
                self_id=self.agent_id, bot_names={},
            )
        prompt_text = prompt_text.replace("{post_type_menu}", post_type_menu)

        messages = [{"role": "user", "content": prompt_text}]
        return system_prompt, messages

    # ------------------------------------------------------------------
    # Working memory update
    # ------------------------------------------------------------------

    def update_working_memory_file(
        self,
        new_memory: str,
        visibility: str = VISIBILITY_PUBLIC,
        channel_id: str | None = None,
    ) -> None:
        """Write working memory to the visibility-scoped segment.

        Public memory → profiles/memory/{agent_id}/public.md.
        Private memory → profiles/memory/{agent_id}/private/{channel_id}.md
        (requires channel_id). See specs/privacy-and-channel-visibility.md §G2.
        """
        if visibility == VISIBILITY_COLLAB_PRIVATE:
            if not channel_id:
                logger.error("[%s] Private memory update missing channel_id", self.agent_id)
                return
            memory_path = (
                PROFILES_DIR / "memory" / self.agent_id / "private" / f"{channel_id}.md"
            )
        else:
            memory_path = PROFILES_DIR / "memory" / self.agent_id / "public.md"
        try:
            memory_path.parent.mkdir(parents=True, exist_ok=True)
            memory_path.write_text(new_memory + "\n", encoding="utf-8")
            # Best-effort cleanup of the legacy unpartitioned file so subsequent
            # loads go through the new path — only on public writes, and only
            # if we just wrote to the partitioned location.
            if visibility == VISIBILITY_PUBLIC:
                legacy = PROFILES_DIR / "memory" / f"{self.agent_id}.md"
                if legacy.exists():
                    try:
                        legacy.unlink()
                    except OSError as exc:
                        logger.warning(
                            "[%s] Could not remove legacy memory file %s: %s",
                            self.agent_id, legacy, exc,
                        )
                self._public_working_memory = None  # invalidate public cache
            # Private segments are not cached, so no invalidation needed.
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


# Fallback identity block used only if prompts/identity.md (or a role's
# override) is missing from disk. Must match prompts/identity.md verbatim,
# including the absence of a trailing newline — see _compose_system_prompt,
# which relies on exactly one blank line separating this block from its
# neighbors.
_DEFAULT_IDENTITY = """## Your Identity
You are **{bot_name}**, the AI agent representing the {pi_name} lab.
Your agent ID is "{agent_id}". When communicating, represent your lab professionally."""


def _default_system_prompt() -> str:
    """Emergency fallback used only if prompts/agent-system.md (or a role override)
    cannot be loaded from disk. Not the real prompt -- keep this short and generic;
    see prompts/agent-system.md for the actual behavior contract."""
    return """You are an AI agent representing a research lab in a Slack workspace run by
Blackbird Laboratories. Your job is to pitch your own lab's best research to
BlackbirdBot, Blackbird's scouting hub, and to answer its screening questions honestly.

## Core Rules
1. **Represent your lab honestly.** Only claim capabilities, techniques, results, and
   stages of evidence that are real. Never inflate what you have.
2. **You never propose collaborations.** There is no lab-to-lab conversation in this
   workspace — every conversation is between your agent and the hub.
3. **Defer PI-intent questions.** For funding preference, appetite for equity, or any
   other decision only your PI can make, say you'd need to check with your PI rather
   than answering on their behalf.
4. **Cannot commit effort, resources, or funding decisions on behalf of your PI.**"""
