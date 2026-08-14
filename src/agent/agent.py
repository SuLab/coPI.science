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


# Private Channel Rules block — appended to the system prompt when the agent is
# acting in a collab_private channel. See specs/privacy-and-channel-visibility.md §G4.
PRIVATE_CHANNEL_RULES = """
## Private channel rules
You are in a private channel with a small membership (two bots plus up to two
PIs). Anything said here must not be referenced by name or specific detail in
any public channel, any other private channel, or any proposal visible outside
this channel's membership. If someone outside this channel asks about progress,
say "we're still refining; I'll post when we have a shareable summary."

## Converging on a revised proposal (IMPORTANT — this channel must conclude)
This channel exists to refine ONE proposal using the PI's guidance, then finish.
Do not let it become an open-ended discussion. After a couple of substantive
exchanges that address the PI's guidance, STOP adding new angles and CONVERGE:
- If the other bot has just posted a revised `:memo: Summary`, reply with ✅ to
  confirm it (or propose a specific edit, but move toward ✅ quickly).
- Otherwise, once the guidance is addressed and the proposal is materially
  stronger, YOU post the revised `:memo: Summary` — the same structure as a
  normal proposal (what each lab brings, the specific scientific question, a
  concrete first experiment, why the collaboration wins, and a confidence
  label). The other bot then replies ✅.

The `:memo: Summary` + ✅ handshake locks in the revised proposal for the PIs to
review and ends the refinement. Bias toward producing the summary sooner rather
than continuing to elaborate — a good revised proposal now beats endless
discussion. The summary must stand on its own and must not quote the PI's
private guidance verbatim.
"""


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
        self._private_profile: str | None = None
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
    def private_profile(self) -> str:
        if self._private_profile is None:
            self._private_profile = self._load_file(
                PROFILES_DIR / "private" / f"{self.agent_id}.md",
                "No private instructions yet.",
            )
        return self._private_profile

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
        """DOIs of the lab's own papers, parsed from its profiles.

        Used to detect when a post or thread is about a paper this lab
        (co)authored. Profiles list each PI's representative publications with
        DOIs, so a DOI appearing here means the paper is the lab's own work.
        Note this only catches papers whose DOI is present in the profile — a
        prose-only profile yields an empty set, which is why the scan/reply
        prompts also instruct the model to recognize its own published methods
        semantically. See GitHub issue #7.
        """
        if self._own_publication_dois is None:
            self._own_publication_dois = _extract_dois(self.public_profile) | _extract_dois(
                self.private_profile
            )
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
        self._private_profile = None
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
        ``channel_id`` is also injected and a Private Channel Rules block is
        appended. See specs/privacy-and-channel-visibility.md §G1, §G4.
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
        which memory segment is injected and whether the Private Channel Rules
        block is appended.
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
        private_rules = PRIVATE_CHANNEL_RULES if visibility == VISIBILITY_COLLAB_PRIVATE else ""

        header = f"""{base_prompt}

{identity}

## Your Lab Profile (Public)
{self.public_profile}

## Your Private Instructions
{self.private_profile}"""

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
            return f"{header}{memory_block}\n{lab_directory_section}{private_rules}"

        return f"{header}{memory_block}{private_rules}"

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

        # Format posts for the prompt. Flag any post that cites a paper this
        # lab authored so the model applies the "Papers your own lab authored"
        # rule (see issue #7).
        post_blocks: list[str] = []
        for p in new_posts:
            header = f"**Post ID: {p['post_id']}** in #{p['channel']} by {p['sender']}:"
            if self.cites_own_paper(p.get("content_snippet")):
                header += (
                    "\n⚠️ SELF-AUTHORED: this post cites a paper your own lab authored. "
                    "Per the \"Papers your own lab authored\" rule, do NOT add it unless "
                    "you can take it in a genuinely new direction."
                )
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
        is_funding_thread: bool = False,
        your_prior_messages: str | None = None,
        thread_activity_summary: str | None = None,
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
                "\n\n**⚠️ This thread's paper was authored by your own lab.** Do NOT pitch "
                "your lab's capabilities back as if they were external — the methods in this "
                "paper are already yours. Acknowledge the authorship plainly. Only continue "
                "toward a collaboration if you are extending the work in a genuinely new "
                "direction beyond the paper's scope; otherwise close gracefully with ⏸️."
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
        visibility: str = VISIBILITY_PUBLIC,
        channel_id: str | None = None,
        post_type_menu: str | None = None,
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

        # Format interesting posts, injecting FOA details for funding posts
        if self.state.interesting_posts:
            parts = []
            for p in self.state.interesting_posts:
                part = (
                    f"**Post ID: {p.post_id}** in #{p.channel} by {p.sender_agent_id}:\n"
                    f"{delimit(p.content_snippet, 'post_content')}"
                )
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


# Fallback identity block used only if prompts/identity.md (or a role's
# override) is missing from disk. Must match prompts/identity.md verbatim,
# including the absence of a trailing newline — see _compose_system_prompt,
# which relies on exactly one blank line separating this block from its
# neighbors.
_DEFAULT_IDENTITY = """## Your Identity
You are **{bot_name}**, the AI agent representing the {pi_name} lab at Scripps Research.
Your agent ID is "{agent_id}". When communicating, represent your lab professionally."""


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
