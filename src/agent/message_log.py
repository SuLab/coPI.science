"""Global append-only message log — single source of truth for the simulation."""

import bisect
import logging
import re
from dataclasses import dataclass
from typing import Callable

from src.visibility import VISIBILITY_COLLAB_PRIVATE

logger = logging.getLogger(__name__)

# `AgentMessage.phase` for a hub panel note — the one-line, signal-level trace
# the scout hub posts into an interview thread when a specialist consult
# succeeds (src/agent/simulation.py::_post_panel_note). It is a real message on
# the transport and a real `agent_messages` row, so it lives in this log like
# any other post: that is what keeps the append idempotency, the Slack-mirror
# dedup (`_known_slack_ts`) and the DB persist callback working for it.
#
# It is NOT conversation. Agents must never see it — not in a thread history,
# not as a trigger to reply, not in a message count, not in a memory synthesis
# — which is why every list-returning read below filters it out and why no
# prompt file had to change to keep it out of a prompt. See `is_panel_note`.
PHASE_PANEL_NOTE = "panel_note"


@dataclass
class LogEntry:
    """A single message in the global log."""

    ts: str  # Slack message timestamp (unique ID)
    channel: str
    sender_agent_id: str | None  # None for human PI messages
    sender_name: str
    content: str
    thread_ts: str | None = None  # None for top-level posts
    posted_at: float = 0.0  # Unix timestamp (float(ts))
    is_bot: bool = True
    # Visibility class of the channel this entry was posted in. Drives memory-
    # synthesis filtering (G2) — private-channel entries never feed the public
    # memory segment. Default 'public' is safe for all existing callers.
    # See specs/privacy-and-channel-visibility.md §G2.
    visibility: str = "public"
    # Slack-mirror mapping — set when this message was posted to (or came from)
    # Slack. In pure Slack-on mode slack_ts == ts. Persisted to the DB row so the
    # reconcile pass can dedup a mirrored message. See specs/local-db-conversations.md.
    slack_ts: str | None = None
    slack_channel_id: str | None = None
    # The *root's* Slack ts for a mirrored reply. Distinct from thread_ts, which
    # is the canonical (possibly locally-minted) root id: a thread that started
    # Slack-off has a minted root, which is not a valid Slack ts. None means this
    # entry has no Slack parent — either it is not a reply, or its thread has no
    # Slack presence. See SimulationEngine._slack_parent_ts.
    slack_thread_ts: str | None = None
    # What KIND of message this is, mirroring `agent_messages.phase`. None means
    # "derive it from the shape" — `_flush_persisted` writes 'thread_reply' for
    # an entry with a thread_ts and 'new_post' otherwise, which is what every
    # pre-existing caller wants and gets by leaving this unset. It is set
    # explicitly only for a message that is NOT conversation:
    # PHASE_PANEL_NOTE. Restored from the row on every DB-origin ingest path
    # (`_rebuild_state_from_db`, `_hydrate_thread_from_db`,
    # `_poll_inbound_from_db`) so the exclusions below survive a restart.
    phase: str | None = None


def is_panel_note(entry: "LogEntry") -> bool:
    """Whether this entry is a hub panel note — see PHASE_PANEL_NOTE.

    Every list-returning read on ``MessageLog`` drops these, GATED and UNGATED
    alike. The exclusion is not a cohort question and does not belong to
    ``_entry_allowed``: a panel note is not conversation for ANY agent,
    including the hub that wrote it, so there is no gate setting under which it
    should be returned.
    """
    return entry.phase == PHASE_PANEL_NOTE


def _entry_allowed(entry: "LogEntry", allowed_sender_ids: set[str] | None) -> bool:
    """Cohort gate for one log entry. See .notes/cohort-system-v2.md §5.1.

    Returns True (entry is visible to the viewing agent) when:

    - ``allowed_sender_ids is None`` — the gate is off for this agent (isolation
      disabled, or ``cohort_default_policy="open"`` and the agent is uncohorted);
    - the author is a **human** — keyed on ``is_bot``, *not* on
      ``sender_agent_id is None``. ``agent_messages.agent_id`` is nullable, so a
      bot-authored row written with a NULL agent_id ingests through
      ``_poll_inbound_from_db`` as ``sender_agent_id=None`` and would otherwise
      pass the gate as a human;
    - the entry is in a ``collab_private`` channel — a PI explicitly paired those
      two agents via the reopen flow, and an admin-level grouping must not veto an
      explicit human pairing. Read from the persisted ``LogEntry.visibility``
      rather than the engine's in-memory channel map, so it is correct for rows
      ingested from another process and after a restart;
    - the author shares at least one cohort with the viewing agent.

    Named ``_entry_allowed``, not ``_sender_allowed``: it is no longer a function
    of the sender alone.
    """
    if allowed_sender_ids is None:
        return True
    if not entry.is_bot:
        return True
    if entry.visibility == VISIBILITY_COLLAB_PRIVATE:
        return True
    if entry.sender_agent_id is None:
        # A bot row with no agent_id cannot be attributed to a cohort. Fail closed:
        # unattributable bot traffic must not leak through the human bypass.
        return False
    return entry.sender_agent_id in allowed_sender_ids


class MessageLog:
    """
    Append-only in-memory message log.

    All posts and replies are recorded here. Agents query it to find
    new posts since their last turn, thread histories, etc.

    **Cohort-gate classification (.notes/cohort-system-v2.md §6).** Every public
    read method is classified GATED or UNGATED below, and the classification is
    repeated in each method's docstring. ``tests/unit/test_cohort_isolation.py``
    fails if a new public ``get_*``/``has_*`` method appears without one, so the
    inventory cannot silently rot.

    GATED — takes ``allowed_sender_ids`` and drops entries the viewing agent may
    not act on:
        get_new_top_level_posts, get_replies_to_agent_posts, get_tags_for_agent,
        has_new_reply_from_other

    Decision 5 (2026-08-12 PI-interaction removal cycle) drew a line these four
    methods do NOT all sit on the same side of: a human-authored (``is_bot=False``)
    row stays visible through a general-purpose per-agent READ
    (``get_new_top_level_posts``/``get_replies_to_agent_posts``/
    ``get_tags_for_agent`` — history/observability is kept), but must never drive
    BOT BEHAVIOR — pending state, reactive priority, or thread activation.
    ``has_new_reply_from_other`` is the one method whose entire job IS driving bot
    behavior (it feeds ``_owes_reply``'s reactive-priority tier and
    ``_phase4_reply_threads``'s pending-reply trigger, and has no other caller), so
    it alone filters out human rows unconditionally, independent of
    ``allowed_sender_ids`` — including the ``allowed_sender_ids=None`` case, which
    bypasses ``_entry_allowed`` entirely. The other three GATED methods' only real
    caller today is ``SimulationEngine._phase3_activate_threads`` (thread
    activation), so THAT function — not the shared read — is where the
    activation-inert guard lives; see its own is_bot filtering and docstring.
    ``_entry_allowed``'s own human-bypass clause is untouched throughout (it is a
    general cohort-gate primitive with its own tests — see
    ``test_cohort_isolation.py``'s ``TestGateHelper``).

    UNGATED by design — thread-internal, self-authored, or bookkeeping:
        get_entry, get_thread_history, get_thread_message_count,
        get_agent_top_level_posts, get_last_bot_sender_in_channel,
        get_thread_allowed_agents, latest_timestamp

    Writes (``append`` / ``load_entry`` / ``_record``) are NEVER gated: the log is
    shared by every agent in the process, so filtering at ingest would filter for
    all of them at once. The gate belongs at the per-agent read. See v2 §6.2.

    **Panel notes are excluded from EVERY list-returning read**, GATED and
    UNGATED alike (``is_panel_note`` / ``PHASE_PANEL_NOTE``). One invariant,
    stated once: no read on this class ever hands an agent a panel note. The
    two top-level readers (``get_new_top_level_posts``,
    ``get_agent_top_level_posts``) would also exclude them structurally — a
    panel note is always a threaded reply — but they filter explicitly anyway,
    so the invariant is a property of the class rather than of a coincidence in
    two of its methods.

    The two deliberate exceptions are not agent reads:

    * ``get_entry`` — a single-id lookup that must answer for ANY id in the
      log. It backs the idempotency check in ``append``, the live Slack
      poller's dedup and ``SimulationEngine._slack_parent_ts``; filtering it
      would re-ingest a panel note from Slack as a brand-new inbound message,
      i.e. would produce exactly the leak the exclusions exist to prevent.
    * ``latest_timestamp`` — a cursor high-water mark. A panel note IS a real
      message on the transport, and a cursor that stopped short of one would
      make the poller re-fetch it forever.
    """

    def __init__(self) -> None:
        self._entries: list[LogEntry] = []
        self._by_ts: dict[str, LogEntry] = {}  # ts -> entry for fast lookup
        # Map bot_name (lowercase) -> agent_id, set by SimulationEngine
        self._bot_name_to_id: dict[str, str] = {}
        # Optional persistence hook, invoked once per *new* append. The engine
        # registers this to mirror the log into the DB (the primary store).
        # Kept as a plain callback so this module stays DB-agnostic. See
        # specs/local-db-conversations.md.
        self._persist_cb: Callable[[LogEntry], None] | None = None
        # High-water mark over posted_at, maintained on every add. Insertion
        # order is NOT time order (see _record), so latest_timestamp cannot read
        # the tail of _entries.
        self._max_posted_at: float = 0.0
        # ---- read indexes (audit 2026-08-21 finding 3) -------------------
        # Every read used to scan self._entries in full, synchronously, on
        # the event-loop thread — dozens of scans per main-loop tick over an
        # append-only list (measured 0.7s/tick at 100k entries). The indexes
        # below make each read O(matches). INVARIANTS the indexes must not
        # change: since-readers return matches in INSERTION order; ties in
        # get_last_bot_sender_in_channel keep the LATER insertion;
        # get_thread_history is stable by (posted_at, insertion); panel-note
        # and cohort filters stay at READ time (get_entry must keep seeing
        # notes). tests/unit/test_message_log_differential.py is the contract.
        self._seq_by_ts: dict[str, int] = {}  # ts -> insertion seq
        self._by_thread: dict[str, list[LogEntry]] = {}
        self._by_time: list[tuple[float, int, LogEntry]] = []  # sorted
        # Keyed on sender_agent_id INCLUDING None, so the two per-sender reads
        # stay byte-equivalent to the full scans they replaced for every
        # possible argument — a human top-level row (sender_agent_id=None) was
        # matched by the old `e.sender_agent_id == agent_id` comparison too.
        self._top_level_ts_by_sender: dict[str | None, set[str]] = {}
        self._top_level_by_sender: dict[str | None, list[LogEntry]] = {}
        self._last_bot_in_channel: dict[str, LogEntry] = {}

    def set_bot_name_map(self, mapping: dict[str, str]) -> None:
        """Register bot_name -> agent_id mapping (lowercase keys)."""
        self._bot_name_to_id = dict(mapping)

    def set_persist_callback(self, cb: Callable[[LogEntry], None] | None) -> None:
        """Register a callback fired after each new append (for DB persistence)."""
        self._persist_cb = cb

    def append(self, entry: LogEntry) -> bool:
        """Add a message to the log.

        Idempotent: if an entry with this ts is already present, the append is
        skipped (both the in-memory add and the persist callback) and False is
        returned. This unifies the previously scattered ``get_entry`` guards and
        keeps the DB persist hook from double-writing during Slack reconciliation.
        Safe because ids are unique (Slack ts or a minted ts; see mint_ts).
        Returns True when a new entry was added.

        Loop-only, NOT thread-safe: the dedupe above is a check-then-act (the
        ``entry.ts in self._by_ts`` test and the ``_record`` that follows are two
        separate steps) and ``_record`` mutates two structures (``_entries`` and
        ``_by_ts``) without a lock. Safe to call from coroutines sharing one
        event-loop thread; calling it from a worker thread (e.g. from inside an
        ``asyncio.to_thread`` transport call) can race two appends past the dedupe
        check or interleave the two mutations. Always call this on the loop —
        fetch/post off it, then append back on it.
        """
        if entry.ts in self._by_ts:
            return False
        self._record(entry)
        if self._persist_cb is not None:
            self._persist_cb(entry)
        return True

    def load_entry(self, entry: LogEntry) -> None:
        """Append a restored entry WITHOUT firing the persist callback.

        Used by the DB-rebuild path so rows just read from the DB are not
        re-persisted. Still idempotent on ts.
        """
        if entry.ts in self._by_ts:
            return
        self._record(entry)

    def _record(self, entry: LogEntry) -> None:
        """Store an entry, advance the high-water mark, maintain the indexes.

        The log is append-only in *insertion* order, which is not time order: the
        DB inbound poller and the Slack reconcile append entries whose posted_at
        can predate messages already stored. Every "most recent" query therefore
        has to key on posted_at rather than on the tail of ``_entries``.

        Runs once per unique ts (append/load_entry dedupe first), in
        insertion order — which is what makes the incremental
        ``_last_bot_in_channel`` update below exactly equivalent to the old full
        scan's ``>=`` tie rule.
        """
        seq = len(self._entries)
        self._entries.append(entry)
        self._by_ts[entry.ts] = entry
        self._seq_by_ts[entry.ts] = seq
        if entry.posted_at > self._max_posted_at:
            self._max_posted_at = entry.posted_at
        if entry.thread_ts is not None:
            self._by_thread.setdefault(entry.thread_ts, []).append(entry)
        bisect.insort(self._by_time, (entry.posted_at, seq, entry),
                      key=lambda t: (t[0], t[1]))
        if entry.thread_ts is None:
            self._top_level_ts_by_sender.setdefault(
                entry.sender_agent_id, set()
            ).add(entry.ts)
            self._top_level_by_sender.setdefault(
                entry.sender_agent_id, []
            ).append(entry)
        if entry.is_bot and entry.sender_agent_id and not is_panel_note(entry):
            best = self._last_bot_in_channel.get(entry.channel)
            if best is None or entry.posted_at >= best.posted_at:
                self._last_bot_in_channel[entry.channel] = entry

    def _since(self, since: float) -> list[LogEntry]:
        """Entries with posted_at strictly greater than ``since``, in
        INSERTION order — the same order the old full scans returned.

        Insertion order, not time order, is load-bearing: Phase-3 activation
        follows the order these reads return, so a time-ordered index would
        silently reorder activations (audit 2026-08-21 §F3).
        """
        i = bisect.bisect_right(self._by_time, (since, float("inf")),
                                key=lambda t: (t[0], t[1]))
        tail = self._by_time[i:]
        tail.sort(key=lambda t: t[1])
        return [t[2] for t in tail]

    def get_entry(self, ts: str) -> LogEntry | None:
        """Look up a single entry by its timestamp.

        COHORT-GATE: UNGATED — single-id lookup; callers already know the id.
        """
        return self._by_ts.get(ts)

    def get_new_top_level_posts(
        self,
        since: float,
        channels: set[str],
        exclude_agent_id: str,
        allowed_sender_ids: set[str] | None = None,
    ) -> list[LogEntry]:
        """
        Return top-level posts (thread_ts is None) in the given channels,
        posted after `since`, excluding posts from `exclude_agent_id`.

        When `allowed_sender_ids` is provided, only posts from those agents (plus
        human PI posts) are returned — the cohort gate (see specs/cohort-system.md).
        This is a general-purpose per-agent read: a human row stays visible through
        it for history/observability (decision 5, 2026-08-12 PI-interaction removal
        cycle) exactly like `_entry_allowed`'s own human-bypass clause says. The
        bot-behavior mandate that removal cycle actually enforces — a human row must
        never activate a thread — is enforced at the point activation happens
        (`SimulationEngine._phase3_activate_threads`), not here; see that method's
        own is_bot filtering and docstring.
                COHORT-GATE: GATED via allowed_sender_ids.
        """
        results = []
        for entry in self._since(since):
            if is_panel_note(entry):
                continue
            if entry.thread_ts is not None:
                continue
            if entry.channel not in channels:
                continue
            if entry.sender_agent_id == exclude_agent_id:
                continue
            if not _entry_allowed(entry, allowed_sender_ids):
                continue
            results.append(entry)
        return results

    def get_thread_history(self, thread_ts: str) -> list[LogEntry]:
        """Return all messages in a thread (including the root post), ordered by time.

        Ordered by ``posted_at``, not by log-insertion order. Appends from a
        single process arrive roughly in time order, but the DB inbound poller
        and the Slack reconcile append entries whose posted_at can predate
        messages already in the log — so insertion order would hand the LLM a
        subtly scrambled thread, unlike the Slack-primary rebuild which fetched
        in ts order. The sort is stable, so entries sharing a posted_at keep
        their insertion order. The root is pinned first regardless: it is the
        thread's parent by definition, even if a reply carries an earlier
        posted_at (a writer's clock can run behind — see PI_INBOX_LOOKBACK_S).

        Panel notes are dropped (PHASE_PANEL_NOTE). This is THE read that
        becomes an agent's prompt (``_reply_to_thread`` -> ``thread_history``
        -> ``build_phase4_prompt``), so it is the reason no prompt file had to
        change to keep panel notes out of a prompt.

        COHORT-GATE: UNGATED by design — once a thread is open its full history
        is context, including a partner who has since left the cohort (v2 §8).
        """
        root = self._by_ts.get(thread_ts)
        replies = sorted(
            (
                e for e in self._by_thread.get(thread_ts, [])
                if not is_panel_note(e)
            ),
            # (posted_at, insertion seq) makes the sort explicitly what the old
            # stable sort over insertion-ordered _entries was implicitly.
            key=lambda e: (e.posted_at, self._seq_by_ts[e.ts]),
        )
        result = []
        # The root can never BE a panel note (a note is always posted with a
        # thread_ts, so no thread is ever rooted at one), but the guard is
        # written out anyway: the invariant is "this method never returns a
        # panel note", not "it happens not to".
        if root and not is_panel_note(root):
            result.append(root)
        result.extend(replies)
        return result

    def get_thread_message_count(self, thread_ts: str) -> int:
        """Count total messages in a thread (root + replies).

        Panel notes do not count. This number is the interview's turn budget:
        ``_reply_to_thread`` assigns it to ``thread.message_count``, which
        drives both ``phase4_guidance``'s EXPLORE/DECIDE/CONCLUDE ordinal and
        the ``max_thread_messages`` close — so counting notes would spend a
        6-consult interview's turns on its own bookkeeping and conclude it six
        messages early.

        COHORT-GATE: UNGATED by design — bookkeeping over one thread.
        """
        count = 1 if thread_ts in self._by_ts else 0
        count += sum(
            1 for e in self._by_thread.get(thread_ts, [])
            if not is_panel_note(e)
        )
        return count

    def get_agent_top_level_posts(self, agent_id: str, limit: int = 10) -> list[LogEntry]:
        """Return the agent's ``limit`` newest top-level posts, oldest first.

        "Newest" is by ``posted_at``, not by position in the log: a late append of
        older history (DB poll / Slack reconcile — see _record) would otherwise
        push a genuinely recent post out of the slice, which silently weakens both
        callers — the Phase 5 dedup context and the daily post cap.
                COHORT-GATE: UNGATED by design — the agent's own posts.
        """
        posts = sorted(
            (
                e for e in self._top_level_by_sender.get(agent_id, [])
                if not is_panel_note(e)
            ),
            key=lambda e: (e.posted_at, self._seq_by_ts[e.ts]),
        )
        return posts[-limit:]

    def get_last_bot_sender_in_channel(self, channel_name: str) -> str | None:
        """Return the agent_id of the most recent bot-authored message in a channel.

        Returns None if no bot has posted there yet. Used to enforce
        turn-taking in flat collab_private channels (a bot shouldn't post
        back-to-back there without the other bot responding first).

        "Most recent" is by ``posted_at``. Scanning ``reversed(_entries)`` instead
        would let a late-appended *older* message answer as the last poster and
        hand the turn to the wrong bot. Ties keep the later insertion, matching
        the previous behaviour when posted_at values collide.
                COHORT-GATE: UNGATED by design — turn-taking within one channel, and the
        only callers are collab_private channels, which the gate exempts (v2 §7).
        """
        # Maintained incrementally by ``_record``, which applies the very same
        # filters (a note is not a turn: letting one answer as "the last
        # poster" would hand the turn to the other bot in a flat private
        # channel on the strength of the hub's own bookkeeping) and the very
        # same ``>=`` tie rule, in insertion order — so this is the old full
        # scan's fold, precomputed.
        best = self._last_bot_in_channel.get(channel_name)
        return best.sender_agent_id if best else None

    def get_replies_to_agent_posts(
        self,
        agent_id: str,
        since: float,
        allowed_sender_ids: set[str] | None = None,
    ) -> list[LogEntry]:
        """
        Find replies (since cursor) to top-level posts authored by agent_id,
        where the reply is from a different agent.

        When `allowed_sender_ids` is provided, replies from non-cohort agents are
        excluded (the cohort gate; human PI replies always pass — decision 5,
        2026-08-12 PI-interaction removal cycle: this is a general-purpose
        per-agent read, and a human row stays visible through it for
        history/observability, same as `_entry_allowed`'s own human-bypass
        clause). The bot-behavior mandate that removal cycle enforces — a human
        reply must never activate a thread — is enforced at the point activation
        happens (`SimulationEngine._phase3_activate_threads`), not here.
                COHORT-GATE: GATED via allowed_sender_ids.
        """
        # First, find all top-level posts by this agent
        agent_post_ts = self._top_level_ts_by_sender.get(agent_id, set())
        results = []
        for entry in self._since(since):
            if is_panel_note(entry):
                continue
            if entry.thread_ts not in agent_post_ts:
                continue
            if entry.sender_agent_id == agent_id:
                continue
            if not _entry_allowed(entry, allowed_sender_ids):
                continue
            results.append(entry)
        return results

    def get_tags_for_agent(
        self,
        agent_bot_name: str,
        since: float,
        allowed_sender_ids: set[str] | None = None,
    ) -> list[LogEntry]:
        """
        Find posts/replies that mention (tag) the given agent bot name,
        posted since the given cursor.

        When `allowed_sender_ids` is provided, tags authored by non-cohort agents
        are excluded (the cohort gate; human PI tags always pass — decision 5,
        2026-08-12 PI-interaction removal cycle: this is a general-purpose
        per-agent read, and a human row stays visible through it for
        history/observability, same as `_entry_allowed`'s own human-bypass
        clause). The bot-behavior mandate that removal cycle enforces — a human
        @-mention must never activate a thread, including via the substring-match
        trap `SimulationEngine._infer_agent_id` could otherwise walk into (e.g.
        "Andrew Su (PI)" contains agent_id "su") — is enforced at the point
        activation happens (`_phase3_activate_threads`), not here.
                COHORT-GATE: GATED via allowed_sender_ids.
        """
        tag = f"@{agent_bot_name}".lower()
        results = []
        for entry in self._since(since):
            if is_panel_note(entry):
                # The note quotes the hub's own question back, so a bot name
                # inside that question would otherwise read as an @-mention and
                # activate a thread on the strength of the hub's bookkeeping.
                continue
            if not _entry_allowed(entry, allowed_sender_ids):
                continue
            if tag in entry.content.lower():
                results.append(entry)
        return results

    def get_thread_allowed_agents(self, thread_ts: str) -> set[str] | None:
        """Return the set of agent_ids allowed to participate in this thread.

        Rules:
        - If the root post tags a specific agent, only the poster and tagged
          agent may participate → returns {poster, tagged}.
        - If no tag, falls back to generic 2-party rule: the first two distinct
          agents to post are the only allowed participants.
        - Returns None if the thread root is not found.

        Panel notes are excluded transitively, via ``get_thread_history``: a
        note is not participation, so a hub that has posted only a note into a
        thread is not yet one of its two parties — exactly the answer this
        returned before notes existed.
                COHORT-GATE: UNGATED by design — thread participation rules, not cohort.
        """
        root = self._by_ts.get(thread_ts)
        if not root:
            return None

        poster_id = root.sender_agent_id

        # Check if root post tags a specific agent (e.g. @WisemanBot)
        tagged_id = self._extract_tagged_agent(root.content)
        if tagged_id and tagged_id != poster_id:
            return {poster_id, tagged_id} if poster_id else {tagged_id}

        # No tag — use generic 2-party rule: first 2 distinct agent_ids in thread.
        # If fewer than 2 participants, the thread is open for anyone to join.
        history = self.get_thread_history(thread_ts)
        participants: list[str] = []
        seen: set[str] = set()
        for entry in history:
            aid = entry.sender_agent_id
            if aid and aid not in seen:
                participants.append(aid)
                seen.add(aid)
            if len(participants) >= 2:
                break
        if len(participants) < 2:
            return None  # Thread still open — anyone can join
        return set(participants)

    def _extract_tagged_agent(self, content: str) -> str | None:
        """Extract a tagged agent_id from message content (e.g. @WisemanBot)."""
        match = re.search(r"@(\w+[Bb]ot)\b", content)
        if match:
            bot_name = match.group(1).lower()
            return self._bot_name_to_id.get(bot_name)
        return None

    def has_new_reply_from_other(
        self,
        thread_ts: str,
        agent_id: str,
        since: float,
        allowed_sender_ids: set[str] | None = None,
    ) -> bool:
        """Check if the other participant posted a new reply since `since`.

        COHORT-GATE: GATED via allowed_sender_ids.

        See .notes/cohort-system-v2.md §6, §8. This is the read that drives
        both the reactive-priority tier (``_owes_reply``) and the Phase 4 reply
        decision, so leaving it ungated made the scheduler prioritise exactly the
        threads the gate had rejected. Callers pass ``allowed_sender_ids=None`` for
        a thread that is already open and not grandfathered — an open conversation
        is entitled to conclude (v2 §8) — and pass the agent's gate otherwise.

        A human-authored (``is_bot=False``) entry is never treated as "a new
        reply from the other participant", regardless of the gate — including
        the ``allowed_sender_ids=None`` (fully open) case, which bypasses
        ``_entry_allowed`` entirely and would otherwise let a human row through
        unconditionally. There is no PI-bot interaction surface left for a
        human reply to set ``has_pending_reply``, grant reactive priority, or
        (via ``_reply_to_thread``'s message-count recompute) shift a thread's
        ordinal (2026-08-12 removal cycle). This closes the loop
        ``post_agent_message``/``reopen_proposal`` (via
        ``src/services/pi_inbox.py::record_pi_message``) used to feed.
        """
        for entry in self._by_thread.get(thread_ts, []):
            if is_panel_note(entry):
                # The load-bearing one for panel notes. A note is authored by
                # the hub, so it passes the `sender_agent_id == agent_id` skip
                # below only for the OTHER party — i.e. it would tell the PI's
                # lab bot "the hub has replied to you" mid-way through the
                # hub's own turn, and pull it into the reply lane to answer a
                # message that is not addressed to it and that it cannot even
                # see (`get_thread_history` drops it too).
                continue
            if entry.posted_at <= since:
                continue
            if entry.sender_agent_id == agent_id:
                continue
            if not entry.is_bot:
                continue
            if not _entry_allowed(entry, allowed_sender_ids):
                continue
            return True
        return False

    @property
    def latest_timestamp(self) -> float:
        """Return the highest posted_at in the log, or 0.0 when it is empty.

        The maximum, not ``_entries[-1].posted_at``: the last-inserted entry is
        not the newest one whenever the DB poller or the Slack reconcile has
        appended older history (see _record). A cursor taken from the tail could
        therefore move *backwards*.
                COHORT-GATE: UNGATED by design — global high-water mark for cursors.
        """
        return self._max_posted_at

    def __len__(self) -> int:
        return len(self._entries)
