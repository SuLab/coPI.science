"""Posting to and polling Slack for the engine: canonical-id minting, the outbound post path with its message-log write, the per-tick channel poll and the resume cursor seed (spec §7.1)."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from src.agent.engine import constants, deps
from src.agent.engine.constants import POLL_ERROR_LOG_INTERVAL
from src.agent.engine.context import EngineContext, TagFilter, ThreadGoneCallback, via
from src.agent.engine.sidecar import _strip_assessment_sidecar
from src.agent.ids import WRITER_ENGINE, TsMinter
from src.agent.message_log import LogEntry
from src.agent.run_marker import is_run_start_marker
from src.agent.slack_client import AgentSlackClient, ThreadNotFound
from src.models.agent_activity import VISIBILITY_COLLAB_PRIVATE, VISIBILITY_PUBLIC

if TYPE_CHECKING:
    from src.agent.engine.channel_directory import ChannelDirectory
    from src.agent.engine.persistence import Persistence

logger = logging.getLogger("src.agent.simulation")


class SlackIO:
    """Outbound posts and the inbound channel poll."""

    agents = via("ctx")
    message_log = via("ctx")
    slack_clients = via("ctx")
    # The write-through flush at the end of `_post_message` needs both.
    session_factory = via("ctx")
    simulation_run_id = via("ctx")
    _channel_id_map = via("_channel_directory")
    _channel_visibility = via("_channel_directory")
    _client_for_channel = via("_channel_directory")
    _resolve_channel_visibility = via("_channel_directory")
    _flush_persisted = via("_persistence")

    OWNED_STATE: tuple[str, ...] = (
        "_poll_cursors",
        "_poll_client_cursor",
        "_last_channel_poll",
        "_poll_error_last_logged",
        "_ts_minter",
    )

    def __init__(
        self,
        ctx: EngineContext,
        *,
        channel_directory: ChannelDirectory,
        persistence: Persistence,
        on_thread_gone: ThreadGoneCallback,
        tag_filter: TagFilter,
    ) -> None:
        self.ctx = ctx
        self._channel_directory = channel_directory
        self._persistence = persistence
        self._on_thread_gone = on_thread_gone
        self._strip_disallowed_tags = tag_filter

        # channel name -> when its poll failure was last reported at WARNING.
        # See _log_poll_error / POLL_ERROR_LOG_INTERVAL.
        self._poll_error_last_logged: dict[str, float] = {}

        # Slack poll cursor: channel_id -> latest ts seen
        self._poll_cursors: dict[str, str] = {}

        # Wall-clock throttles for Slack pollers + round-robin cursor over
        # connected clients, so one agent's token doesn't carry all poll load.
        self._last_channel_poll: float = 0.0
        self._poll_client_cursor: int = 0
        # Monotonic ts-shaped id minter, seeded at DB rebuild. Owns the engine's
        # writer slot so its ids can never collide with the web app's or
        # GrantBot's, which mint into the same agent_messages table from other
        # processes (R1). See mint_ts and src/agent/ids.py.
        self._ts_minter = TsMinter(WRITER_ENGINE)

    def seed_cursor(self, channel_id: str, ts: str) -> None:
        """Advance ``channel_id``'s Slack poll cursor to ``ts`` if it is newer
        (spec §7.2 rule 1). The DB rebuild seeds cursors from stored rows so the
        first poll fetches only genuinely newer messages."""
        cur = self._poll_cursors.get(channel_id, "0")
        if ts > cur:
            self._poll_cursors[channel_id] = ts

    def _next_poll_client(self):
        """Round-robin a connected Slack client for shared-token polling."""
        connected = [
            c for c in self.slack_clients.values() if c and c.is_connected
        ]
        if not connected:
            return None
        client = connected[self._poll_client_cursor % len(connected)]
        self._poll_client_cursor += 1
        return client

    async def _poll_slack_for_bot_messages(self) -> None:
        """Poll all channels for new bot-authored messages; mirror them into the log.

        Renamed from ``_poll_slack_for_human_messages`` (2026-08-12
        PI-interaction removal cycle): a human-authored channel message is no
        longer ingested via Slack at all — there is no PI-bot interaction
        surface left for it to feed (no reopen, no @-tag routing, no directive
        flag), so keeping a human branch here would only have grown the log
        with entries nothing downstream may act on. The remaining job is
        exactly what the name says: mirror another bot's Slack-native post (a
        message this process did not itself write) into the shared
        ``MessageLog``, recording the Slack-mirror mapping so a reply to it
        can still be threaded. See the removal cycle's PI-interaction audit
        map and ``MessageLog``'s GATED-method inventory (human rows are
        filtered there too, independent of this poller).
        """
        if not self.slack_clients:
            return

        now = deps.time.time()
        if now - self._last_channel_poll < constants.CHANNEL_POLL_INTERVAL:
            return
        self._last_channel_poll = now

        default_client = self._next_poll_client()
        if not default_client:
            return

        # Poll seeded channels plus any collab_private channels tracked in
        # _channel_visibility. Skipping non-seeded public channels avoids
        # polling archived/stale channels from prior sims.
        polled_ids = {
            ch_name: ch_id for ch_name, ch_id in self._channel_id_map.items()
            if ch_name in constants.SEEDED_CHANNELS
            or self._channel_visibility.get(ch_name) == VISIBILITY_COLLAB_PRIVATE
        }
        for ch_name, ch_id in polled_ids.items():
            ch_visibility = self._channel_visibility.get(ch_name, VISIBILITY_PUBLIC)
            # Private channels need a member bot; non-members get channel_not_found.
            client = self._client_for_channel(ch_id, default_client)
            if client is None:
                logger.debug(
                    "Skipping poll for private channel #%s — no connected member bot",
                    ch_name,
                )
                continue
            oldest = self._poll_cursors.get(ch_id, "0")
            try:
                messages = await client.apoll_channel_messages(ch_id, oldest=oldest)
                # `msg["thread_ts"]` arrives normalised: Slack sets thread_ts == ts on
                # a parent once it has replies, and the transport nulls that at ingest
                # (slack_client.normalize_inbound_message). Copying it verbatim, as
                # this loop used to, ingested a root as a reply to itself — and
                # get_new_top_level_posts skips anything with a non-null thread_ts, so
                # the post vanished from every reader of that method (e.g. the hub's
                # Phase 3 auto-activation scan) and _rebuild_state_from_db made it
                # permanent. The rule now lives in exactly one place.
                for msg in messages:
                    ts = msg.get("ts", "")
                    # Engine-authored run-start markers are operational
                    # signage, not conversation: never mirror one into the
                    # log, but advance the cursor past it or this tick's
                    # newest message gets re-fetched forever. See
                    # src/agent/run_marker.py (prefix contract).
                    if is_run_start_marker(msg.get("text")):
                        if ts:
                            self._poll_cursors[ch_id] = ts
                        continue
                    user_id = msg.get("user", "")
                    is_bot = bool(msg.get("bot_id") or msg.get("subtype") == "bot_message")

                    if not is_bot and user_id:
                        is_bot = await client.ais_bot_user(user_id)

                    # Bot messages are mirrored into the log so agents can scan
                    # them; a human message is dropped outright — advance the
                    # cursor past it (so it is not re-fetched every tick) but
                    # never append it. There is no PI-bot interaction surface
                    # left for a human channel post to feed.
                    if not is_bot:
                        if ts:
                            self._poll_cursors[ch_id] = ts
                        continue

                    bot_name = msg.get("username", "bot")
                    # Resolve agent_id from bot name
                    bot_agent_id = self.message_log._bot_name_to_id.get(
                        bot_name.lower()
                    )
                    entry = LogEntry(
                        ts=ts,
                        channel=ch_name,
                        sender_agent_id=bot_agent_id,
                        sender_name=bot_name,
                        content=msg.get("text", ""),
                        thread_ts=msg.get("thread_ts"),
                        posted_at=float(ts) if ts else 0.0,
                        is_bot=True,
                        visibility=ch_visibility,
                        # This message came *from* Slack, so record the mirror
                        # mapping. Without it the entry looks DB-origin, and
                        # _slack_parent_ts then reports "no Slack root" for any
                        # thread rooted here — silently keeping every reply off
                        # Slack. The roots this branch ingests are another
                        # workspace bot's posts. Slack-origin ⇒ canonical id
                        # *is* the Slack ts, so the thread parent needs no
                        # translation.
                        slack_ts=ts or None,
                        slack_channel_id=ch_id,
                        slack_thread_ts=msg.get("thread_ts"),
                    )
                    if not self.message_log.get_entry(ts):
                        self.message_log.append(entry)
                    if ts:
                        self._poll_cursors[ch_id] = ts

            except Exception as exc:  # noqa: BLE001
                # This block covers the WHOLE per-channel ingest — the API call,
                # `float(ts)`, `ais_bot_user` and the append — so it is the only
                # place a broken channel is ever reported.
                self._log_poll_error(ch_name, exc)

    def _log_poll_error(self, ch_name: str, exc: Exception) -> None:
        """Report a channel's poll failure — visibly, but at most once per window.

        WARNING rather than DEBUG (production runs at INFO), and once per
        `POLL_ERROR_LOG_INTERVAL` per CHANNEL rather than once per sweep. Per
        channel and not globally: one permanently-broken channel silencing the
        first failure of a second one would hide exactly the event this exists
        to surface. The suppressed repeats still log at DEBUG, so a debug-level
        operator can still see the failure is ongoing rather than resolved.
        """
        now = deps.time.time()
        last = self._poll_error_last_logged.get(ch_name, 0.0)
        if now - last < POLL_ERROR_LOG_INTERVAL:
            logger.debug(
                "Poll for #%s is still failing (warning suppressed for another "
                "%.0fs): %s",
                ch_name, POLL_ERROR_LOG_INTERVAL - (now - last), exc,
            )
            return
        self._poll_error_last_logged[ch_name] = now
        logger.warning(
            "Polling error for #%s (further warnings for this channel "
            "suppressed for %.0fs): %s",
            ch_name, POLL_ERROR_LOG_INTERVAL, exc,
        )

    def mint_ts(self) -> str:
        """Return a monotonic, unique, ts-shaped id (decimal seconds string).

        The canonical message/channel id when there is no Slack ts (Slack-off,
        or a DB-origin message). Monotonicity preserves the posted_at=float(ts)
        ordering the engine relies on; the minter's high-water mark is seeded from
        the rebuild's max(posted_at) so new ids always sort after restored
        history. Uniqueness is what makes the idempotent MessageLog.append safe,
        and it holds across processes too: this minter owns the engine's writer
        slot, disjoint from the web app's and GrantBot's (R1).
        See src/agent/ids.py and specs/local-db-conversations.md.
        """
        return self._ts_minter.mint()

    async def _post_message(
        self,
        agent_id: str,
        channel: str,
        text: str,
        thread_ts: str | None = None,
        phase: str | None = None,
        landed_check: bool = False,
    ) -> str | None:
        """Post a message to Slack and record it in the message log + DB.

        ``landed_check``: passed only for replies and pitches (spec §8.4 AG-6);
        panel notes, headlines and the run-start marker keep today's handling,
        minus the SDK re-POST.

        ``phase`` overrides the KIND stamped on the resulting rows. None (every
        pre-existing caller) keeps the derived value ``_flush_persisted`` has
        always written — 'thread_reply' with a thread_ts, 'new_post' without —
        so this parameter changes nothing for a reply or a post. It is passed
        only by ``_post_panel_note``, as PHASE_PANEL_NOTE, which is what makes
        the resulting rows invisible to every agent-facing MessageLog read (see
        src/agent/message_log.py). Carried on the LogEntry, not applied at
        flush time, so it survives the round trip through the log and back out
        of the DB on the next rebuild.

        Returns the canonical post id (the root chunk's ``ts`` — a real Slack
        ts when a connected client posted, else a locally-minted one; see
        "Canonical id" below), or ``None`` when nothing was actually recorded:
        the text stripped to nothing, or the reply's parent thread was found
        to be deleted — in either case nothing was posted and no log entry was
        written. Callers that count a turn or persist something derived from
        the post (e.g. the opportunity_assessment verdict sidecar, which
        stores this id as ``slack_ts`` for a link back to the post it
        summarises — F7) must check this before doing either. The return value is truthy exactly when a post
        was recorded, so existing callers that only did ``if not posted:`` (or
        ignore the return value entirely) are unaffected by the ``bool`` ->
        ``str | None`` change.
        """
        # Final safety: strip any leaked <slack_message> tags, and any
        # <assessment_json> sidecar — that block is for Blackbird staff and the DB,
        # never for the channel. See _strip_assessment_sidecar for why an
        # unclosed tag is handled differently from a well-formed pair.
        text = _strip_assessment_sidecar(text)
        text = re.sub(r"</?slack_message>", "", text).strip()

        # A sidecar-only or truncated response can strip to nothing — e.g. an
        # unclosed <assessment_json> nested as the entire <slack_message> body,
        # with no real text before it (_ASSESSMENT_UNCLOSED_RE then deletes
        # from the very start of the string). Slack rejects empty text anyway,
        # but bailing here also matters for what happens *after* posting:
        # without this guard, _post_message still mints a ts and writes a
        # LogEntry with content="" and slack_ts=None — a DB row with no
        # corresponding Slack message, breaking the row-count-matches-Slack-
        # message-count invariant documented below, and the caller still
        # counts the turn as published (message_count incremented) even
        # though nothing went out. Return before any of that — no Slack
        # call, no minted ts, no log entry.
        if not text:
            logger.warning(
                "[%s] Suppressed a post to #%s: text was empty after "
                "stripping the assessment sidecar/slack_message tags — likely "
                "a sidecar-only or truncated response with no real message body.",
                agent_id, channel,
            )
            return None

        client = self.slack_clients.get(agent_id)
        agent = self.agents.get(agent_id)

        # Cohort gate, outbound side. Placed here rather than in a phase so it
        # covers every caller — Phase 4 replies, Phase 5 posts, private-channel
        # messages — and cannot be bypassed by a new call site. Idempotent, so the
        # extra Phase 5 pass (which needs the cleaned text locally) is harmless.
        # No-op when the gate is off for this agent. See v2 §9.
        if agent is not None:
            cleaned_text, _ = self._strip_disallowed_tags(text, agent)
            text = cleaned_text or text

        # Slack threads on the *root's Slack ts*, which equals the canonical
        # thread_ts only when the root was born on Slack. A thread started
        # Slack-off has a minted root id — passing that to Slack detaches the
        # reply or errors — so such a reply is kept DB-only rather than mirrored.
        slack_parent = self._slack_parent_ts(thread_ts)
        can_mirror = thread_ts is None or slack_parent is not None

        result: dict | None = None
        if client and client.is_connected and not can_mirror:
            logger.warning(
                "[%s] Not mirroring reply to #%s: thread %s has no Slack root "
                "(started with Slack off). The message is still recorded in the DB.",
                agent_id, channel, thread_ts,
            )
        elif client and client.is_connected:
            try:
                post_kwargs: dict = {"thread_ts": slack_parent}
                if landed_check and isinstance(client, AgentSlackClient):
                    post_kwargs["landed_check"] = True
                    post_kwargs["known_ts"] = self.message_log.newest_own_slack_ts(
                        agent_id, channel, thread_ts,
                    )
                result = await client.apost_message(channel, text, **post_kwargs)
            except ThreadNotFound:
                # Parent was deleted. post_message already cleaned up the
                # orphan top-level post on Slack. Purge the dead thread_ts
                # from state so no one replies to it again. Keyed by the
                # canonical id, which is what the engine's state uses.
                if thread_ts:
                    await self._on_thread_gone(thread_ts)
                logger.warning(
                    "[%s] Skipped reply to deleted thread %s in #%s",
                    agent_id, thread_ts, channel,
                )
                return None
        else:
            logger.info("[%s] MOCK post to #%s: %s...", agent_id, channel, text[:60])

        # One log entry per message that really exists on the transport. Normally
        # that is one; it is several when the text was over Slack's 4000-character
        # per-message limit and the client split it (see
        # AgentSlackClient.post_message). Recording a single row for a post Slack
        # turned into five messages left four of them in Slack with no row at all,
        # and named the row's slack_ts after the *tail* — so _slack_parent_ts
        # threaded replies onto a fragment, posted_at took the tail's clock, and the
        # retired Slack reconcile re-ingested the unrecorded head chunks on the next
        # restart as brand-new inbound messages. The mirror is only in bijection with
        # Slack if the row count matches the message count.
        mirrored = self._mirrored_messages(result, text, slack_parent)

        # Canonical id: the Slack ts when a connected client posted, else a
        # locally-minted ts. Slack ts (when present) is also recorded as the
        # mirror mapping on the entry.
        #
        # `visibility` is stamped from the channel's class. It was previously omitted,
        # so every agent-authored message defaulted to "public" even in a
        # collab_private channel — including the ones written into a PI-created
        # refinement channel. Two readers depend on this field:
        #
        #   - the cohort gate's private-channel exemption (_entry_allowed), which is
        #     how a PI pairing outranks an admin cohort grouping — with the field
        #     unset the exemption never fired, and two agents in different cohorts
        #     could not converse in the channel the PI made for them;
        #   - the G2 memory-synthesis filter, which is meant to keep private-channel
        #     content out of the public memory segment.
        #
        # Found by a real multi-turn run: the private-channel messages persisted with
        # visibility='public' while the AgentChannel row said collab_private.
        # See specs/cohort-system-v2.md §7.
        visibility = self._resolve_channel_visibility(channel)
        sender_name = agent.bot_name if agent else f"{agent_id}Bot"
        root_ts: str | None = None
        for index, message in enumerate(mirrored or [None]):
            slack_ts = message.get("ts") if message else None
            ts = slack_ts or self.mint_ts()
            try:
                posted_at = float(ts)
            except (TypeError, ValueError):
                posted_at = deps.time.time()
            # Chunk 0 keeps the caller's canonical thread id. A continuation chunk of
            # a *root* post hangs off chunk 0 — one logical post stays one top-level
            # post, so the hub's Phase 3 auto-activation scan doesn't see N roots
            # where the author wrote one.
            canonical_parent = thread_ts if (thread_ts or index == 0) else root_ts
            entry = LogEntry(
                ts=ts,
                channel=channel,
                sender_agent_id=agent_id,
                sender_name=sender_name,
                content=(message.get("text") if message else None) or text,
                thread_ts=canonical_parent,
                posted_at=posted_at,
                is_bot=True,
                visibility=visibility,
                slack_ts=slack_ts,
                slack_channel_id=(message.get("channel") if message else None),
                # The parent the transport reports, so the row always describes the
                # message the transport actually made rather than the one we asked for.
                slack_thread_ts=(message.get("thread_ts") if message and slack_ts else None),
                # None for every caller but the panel note — see the docstring.
                # Stamped on EVERY chunk: a split message is several rows for
                # one logical post, and a continuation chunk that lost the
                # phase would be readable by agents while its head was not.
                phase=phase,
            )
            if index == 0:
                root_ts = ts
            # Persisted to agent_messages via the MessageLog append callback
            # (_enqueue_persist → _flush_persisted). The DB is the primary store.
            self.message_log.append(entry)
        # Write-through: the post is not done until its rows are in the database,
        # because a restart restores from the database only. Serialized with every
        # other flush by `_persist_flush_lock`. A failed flush re-queues the
        # entries exactly as the per-tick flush does, and the post still counts as
        # posted: it is on Slack.
        if self.session_factory and self.simulation_run_id:
            await self._flush_persisted()
        return root_ts

    @staticmethod
    def _mirrored_messages(
        result: dict | None, text: str, slack_parent: str | None,
    ) -> list[dict]:
        """Normalise a transport's post result into one record per real message.

        ``AgentSlackClient`` reports ``posted_messages``; a Transport backend that
        never splits need not, so a bare ``{"ts": ..., "channel": ...}`` is read as
        the single message it describes. Returns ``[]`` when nothing was posted,
        which is the signal to mint a local canonical id instead.
        See src/agent/transport.py for the declared contract.
        """
        if not result:
            return []
        posted = result.get("posted_messages")
        if posted:
            return list(posted)
        return [{
            "ts": result.get("ts"),
            "channel": result.get("channel"),
            "text": text,
            "thread_ts": slack_parent,
        }]

    def _slack_parent_ts(self, thread_ts: str | None) -> str | None:
        """Resolve a canonical thread id to the Slack ts Slack must thread on.

        Returns None when the thread has no Slack presence (a DB-origin root
        minted while Slack was off), so callers can skip the mirror instead of
        posting against an id Slack has never seen. Falls back to the canonical
        id when the root is not in the log at all (windowed out by the B2 rebuild
        bound), which preserves the pure-Slack-on behaviour where the canonical
        id *is* the Slack ts. The rebuild populates slack_ts on restored entries,
        so this survives a restart. See specs/local-db-conversations.md.
        """
        if not thread_ts:
            return None
        root = self.message_log.get_entry(thread_ts)
        if root is None:
            return thread_ts
        return root.slack_ts

    async def _seed_slack_cursors_without_ingest(self) -> None:
        """Advance the Slack poll cursors past all existing history, ingesting none.

        What `_restore_slack_state` runs on every start, fresh or resumed. Reads
        the channels the live poller reads, and for each one moves
        `_poll_cursors` to the newest timestamp present, so the first poll tick
        asks Slack only for messages posted after this start.

        The cursor is the ONLY thing standing between a start and the whole back
        catalogue: `_poll_slack_for_bot_messages` dedups against
        `message_log.get_entry(ts)`, which holds only this run's stored rows, so
        it would re-append every other message it fetched.

        Channel history is top-level-only, so a pre-start THREAD REPLY can carry
        a ts above the cursor this leaves. Nothing reads it: the live poller uses
        the same top-level-only endpoint, and no code fetches thread replies
        since the Slack reconcile was retired — a Slack-native reply posted while
        the engine was down is not recovered.

        **No channel this pass touches may end with a "0" cursor**, and there are
        three ways it used to:

        1. `AgentSlackClient.get_full_channel_history` CATCHES `SlackApiError`
           and returns `[]`, so the `try/except` below never fires for the
           commonest failure there is. The channel looked empty, the cursor
           stayed "0", and the live poller — a different endpoint, which does
           NOT swallow — re-imported the whole back catalogue on the first tick
           (harness: 30 messages).
        2. A genuinely empty read, indistinguishable from (1) from here.
        3. `_client_for_channel(...) is None`: a private channel with no
           connected member bot, previously a bare `continue`.

        All three now fall back to a WALL-CLOCK ts. That is a deliberate, small
        trade: it is derived from this process's clock rather than Slack's, so a
        clock skew could hide a message posted in the same second as the seed.
        Weighed against re-importing an entire channel's history into a run that
        asked to start clean, and against the fact that this branch only runs
        when we could not read the channel at all, that is the better failure.
        """
        # `_next_poll_client()`, not `next(iter(self.slack_clients.values()))`:
        # the latter picks whatever client happens to be first in the dict, and
        # if THAT one is disconnected the whole seed was skipped — while the live
        # poller, which does use `_next_poll_client`, kept polling happily.
        default_client = self._next_poll_client()
        if not default_client:
            logger.info("No Slack client available — skipping the start-up cursor seed")
            return

        polled_ids = {
            ch_name: ch_id for ch_name, ch_id in self._channel_id_map.items()
            if ch_name in constants.SEEDED_CHANNELS
            or self._channel_visibility.get(ch_name) == VISIBILITY_COLLAB_PRIVATE
        }
        # One wall clock for the whole pass, so every unreadable channel gets the
        # same baseline and the number is not a per-channel accident.
        now_ts = f"{deps.time.time():.6f}"
        channels = 0
        skipped = 0
        unreadable = 0

        def _fallback(ch_id: str) -> None:
            if self._poll_cursors.get(ch_id, "0") == "0":
                self._poll_cursors[ch_id] = now_ts

        for ch_name, ch_id in polled_ids.items():
            client = self._client_for_channel(ch_id, default_client)
            if client is None:
                logger.warning(
                    "Start-up cursor seed: no connected member bot for "
                    "private channel #%s — parking its cursor at the wall clock "
                    "rather than 0", ch_name,
                )
                _fallback(ch_id)
                unreadable += 1
                continue
            try:
                messages = await client.aget_full_channel_history(ch_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Start-up cursor seed failed for #%s: %s", ch_name, exc,
                )
                _fallback(ch_id)
                unreadable += 1
                continue
            newest = ""
            for msg in messages:
                ts = msg.get("ts", "")
                if not ts:
                    continue
                skipped += 1
                if ts > newest:
                    newest = ts
            if newest:
                cur = self._poll_cursors.get(ch_id, "0")
                if newest > cur:
                    self._poll_cursors[ch_id] = newest
                channels += 1
            else:
                # Empty, or an error the client swallowed into an empty list —
                # from here they are the same observation, and only one of them
                # is safe to leave at "0".
                _fallback(ch_id)
                unreadable += 1
        logger.info(
            "Start-up cursor seed: ignoring %d pre-existing Slack message(s) across %d "
            "channel(s); poll cursors advanced to the current head "
            "(%d channel(s) unreadable or empty, parked at the wall clock)",
            skipped, channels, unreadable,
        )
