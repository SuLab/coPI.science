"""Migration service: public thread → collab_private channel.

Implements the v1 Migration Rule from specs/privacy-and-channel-visibility.md
§"When Channels Become Private". Called by the PI Reopens a Proposal flow
(``POST /agent/{agent_id}/proposals/{thread_decision_id}/reopen``) to move a
thread from its public origin into a new collab_private channel before any PI
guidance text is posted.

The service orchestrates:
  1. Slack: create a private channel, invite the other bot and the triggering PI.
  2. Slack: post a handover message (proposal summary + PI guidance verbatim)
     in the new channel.
  3. Slack: post a neutral ⏸️ marker in the origin thread. **No PI text is
     echoed into the origin thread.**
  4. Slack: DM the other PI (if they exist) from their own bot with an
     invite/pointer to the new channel. The other PI's acceptance is optional —
     refinement proceeds regardless.
  5. DB: insert an AgentChannel row with visibility='collab_private' and
     migrated_from_channel_id pointing at the origin channel.
  6. DB: insert PrivateChannelMember rows for both bots and the triggering PI.
     The second PI is NOT recorded as a member until they actually join.
  7. DB: update thread_decisions.refined_in_channel.

Slack-side side-effects are performed before DB writes so a Slack failure
aborts cleanly without leaving a stale AgentChannel row. If DB writes fail
after Slack succeeds, we log — the orphan Slack channel can be archived manually.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.channels import normalize_channel_name
from src.agent.ids import mint_local_ts
from src.agent.slack_client import AgentSlackClient, ThreadNotFound
from src.config import get_settings
from src.models import (
    VISIBILITY_COLLAB_PRIVATE,
    VISIBILITY_PUBLIC,
    AgentChannel,
    AgentMessage,
    AgentRegistry,
    PrivateChannelMember,
    SimulationRun,
    ThreadDecision,
    User,
)

logger = logging.getLogger(__name__)

# Neutral marker closing the origin public thread. Deliberately carries none of
# the PI's guidance text — that stays inside the private channel (§G6).
_CLOSE_MARKER_TEXT = "⏸️ continuing this discussion off-channel."


@dataclass
class MigrationResult:
    channel_id: str           # Slack channel ID of the new private channel
    channel_name: str         # Slug, e.g., priv-su-wiseman-drug-repurposing
    agent_channel_id: uuid.UUID
    invited_other_pi: bool    # whether we DM'd the other agent's PI


def _build_slug(agent_a: str, agent_b: str, origin_channel_name: str) -> str:
    """Descriptive private-channel slug.

    Form: ``priv-{alpha}-{beta}-{origin}`` where alpha/beta are lowercase
    agent IDs sorted alphabetically (so the slug is stable regardless of
    which bot creates the channel). Origin-channel name is included as a
    readability hint for PIs browsing Slack's channel list.

    Slack limits channel names to 80 chars, lowercase, alphanumeric + hyphens.
    We use the shared normalize helper to guarantee compliance. The spec
    (G6) accepts the trade-off that this leaks collaboration metadata — the
    usability win for PIs outweighs the concern.
    """
    a, b = sorted([agent_a.lower(), agent_b.lower()])
    raw = f"priv-{a}-{b}-{origin_channel_name}"
    return normalize_channel_name(raw)


# Slack's chat.postMessage enforces a ~4000-char text limit (without blocks);
# anything longer is silently split or truncated on some paths. Build the
# handover as 2-3 deliberate top-level messages to keep each comfortably
# under the limit with clean content boundaries. See observed split on
# priv-lotz-su-single-cell-omics where a single ~4600-char handover landed
# as two unrelated-looking posts (one orphaned mid-bullet).
#
# Kept below slack_client.SLACK_MAX_TEXT_CHARS deliberately: _add_handover_message
# writes ONE DB row per call, so a post that Slack splits would desynchronise the
# mirror (8515f65, defect 2). Pinned by
# tests/unit/test_slack_client_contract.py::test_handover_post_budget_stays_under_the_slack_split_threshold.
_MAX_POST_CHARS = 3500


def _build_handover_messages(
    creator_pi_name: str,
    proposal_summary: str | None,
    guidance_text: str,
    origin_channel_name: str,
) -> list[str]:
    """Return the sequence of top-level posts that together form the handover.

    Posts (in order):
      1. Header + proposal summary.
      2. PI guidance (split into multiple posts if necessary to stay under
         _MAX_POST_CHARS).
      3. Closing "bots, please proceed" prompt.

    Every returned post is guaranteed to be under _MAX_POST_CHARS characters.
    """
    summary_block = proposal_summary.strip() if proposal_summary else "_(no summary recorded)_"
    header = (
        f"*Private refinement channel*\n\n"
        f"This channel was created because {creator_pi_name} reopened the proposal "
        f"with guidance. The original thread was in #{origin_channel_name}; "
        f"further discussion will happen here so their guidance stays within this "
        f"channel's membership.\n\n"
        f"*Proposal summary:*\n{summary_block}"
    )
    guidance_posts = _chunk_guidance(creator_pi_name, guidance_text.strip())
    closing = "Continuing the conversation here — bots, please proceed with refinement."

    posts = [header, *guidance_posts, closing]
    # Defensive: ensure no single chunk exceeds the limit. If the header
    # itself somehow does (summary way too long), hard-truncate with a marker.
    return [p if len(p) <= _MAX_POST_CHARS else p[: _MAX_POST_CHARS - 20] + "\n…(truncated)" for p in posts]


def _chunk_guidance(creator_pi_name: str, guidance_text: str) -> list[str]:
    """Split guidance into 1+ posts, breaking on paragraph boundaries."""
    header_prefix = f"*Guidance from {creator_pi_name}"  # " (1 of N):*\n..."
    budget_per_post = _MAX_POST_CHARS - len(header_prefix) - 20  # leave room for "(N of M):*\n"

    if len(guidance_text) + len(header_prefix) + 4 <= _MAX_POST_CHARS:
        return [f"*Guidance from {creator_pi_name}:*\n{guidance_text}"]

    # Split on blank lines first, then on single newlines, then on sentence
    # boundaries as a last resort.
    paragraphs = guidance_text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if not current:
            current = para
        elif len(current) + 2 + len(para) <= budget_per_post:
            current = f"{current}\n\n{para}"
        else:
            chunks.append(current)
            current = para
    if current:
        chunks.append(current)

    total = len(chunks)
    return [
        f"*Guidance from {creator_pi_name} ({i+1} of {total}):*\n{chunk}"
        for i, chunk in enumerate(chunks)
    ]


def _build_other_pi_dm(
    other_pi_name: str,
    creator_pi_name: str,
    origin_channel_name: str,
    new_channel_name: str,
) -> str:
    return (
        f"Hi {other_pi_name.split()[0]} — {creator_pi_name} just reopened the "
        f"proposal our agents drafted in #{origin_channel_name} and asked to "
        f"refine it privately. I've been added to the new channel "
        f"#{new_channel_name}, and you're invited too. Accept the invite in "
        f"Slack to see the full discussion; I'll continue refining in the "
        f"meantime under your standing instructions and will ping you if I "
        f"need input."
    )


async def _get_or_fail_bot_token(db: AsyncSession, agent_id: str) -> str:
    from src.services.slack_tokens import get_agent_bot_token
    tok = await get_agent_bot_token(db, agent_id)
    if not tok:
        raise RuntimeError(f"No valid Slack bot token for agent '{agent_id}'")
    return tok


def _make_client(agent_id: str, bot_token: str) -> AgentSlackClient:
    """Construct and authenticate an AgentSlackClient. Raises if auth fails."""
    client = AgentSlackClient(agent_id=agent_id, bot_token=bot_token)
    if not client.connect():
        raise RuntimeError(f"Failed to authenticate Slack client for agent '{agent_id}'")
    return client


async def _latest_simulation_run_id(db: AsyncSession) -> uuid.UUID:
    """Return the most recent SimulationRun.id — required for the AgentChannel FK.

    AgentChannel rows are scoped to a run historically. A migration from the web
    UI happens between runs, so we attach to the most recent one. If none
    exists (fresh install), raise — the reopen flow is unreachable without a
    prior run anyway.
    """
    result = await db.execute(
        select(SimulationRun.id).order_by(desc(SimulationRun.started_at)).limit(1)
    )
    run_id = result.scalar_one_or_none()
    if run_id is None:
        raise RuntimeError("No SimulationRun exists — cannot attach new AgentChannel")
    return run_id


async def _slack_parent_ts_from_db(
    db: AsyncSession, run_id: uuid.UUID, thread_ts: str,
) -> str | None:
    """Resolve a canonical thread id to the Slack ts Slack must thread on.

    The DB-side twin of ``SimulationEngine._slack_parent_ts``: this process has no
    MessageLog, so the root's mapping is read from ``agent_messages``. Returns None
    when the thread has no Slack presence (a root minted while Slack was off), so
    the caller can skip the mirror instead of posting against an id Slack has never
    seen. ``slack_ts`` is the only evidence — a NULL means not on Slack; see
    ``simulation._restored_slack_ts`` for why a missing mapping is no longer
    inferred from the channel id. See specs/local-db-conversations.md.
    """
    row = (await db.execute(
        select(AgentMessage.slack_ts)
        .where(
            AgentMessage.simulation_run_id == run_id,
            AgentMessage.message_ts == thread_ts,
        )
        .limit(1)
    )).first()
    if row is None:
        # Root not stored at all (a run that predates content persistence). Fall
        # back to the canonical id, preserving pure-Slack-on behaviour where the
        # canonical id and the Slack ts are the same string.
        return thread_ts
    return row[0]


def _add_handover_message(
    db: AsyncSession,
    *,
    simulation_run_id: uuid.UUID,
    agent_id: str,
    channel_id: str,
    channel_name: str,
    content: str,
    visibility: str,
    result: dict | None = None,
    thread_ts: str | None = None,
    slack_thread_ts: str | None = None,
) -> None:
    """Record one handover message in ``agent_messages`` — the primary store.

    Used by both migration paths, so a handover exists in the DB whether or not
    Slack is in play. The canonical id is the Slack ts when the mirror post landed,
    else a locally-minted one — the same rule as ``SimulationEngine._post_message``,
    which means a failed (or skipped) Slack post still leaves the message durable
    and visible to the running simulation. The ``slack_*`` columns are only
    populated when the post actually landed. See specs/local-db-conversations.md.
    """
    slack_ts = (result or {}).get("ts")
    ts = slack_ts or mint_local_ts()
    db.add(AgentMessage(
        simulation_run_id=simulation_run_id,
        agent_id=agent_id,
        channel_id=channel_id,
        channel_name=channel_name,
        message_ts=ts,
        message_length=len(content),
        thread_ts=thread_ts,
        phase="thread_reply" if thread_ts else "new_post",
        visibility=visibility,
        content=content,
        sender_name=f"{agent_id}Bot",
        is_bot=True,
        posted_at=float(ts),
        slack_ts=slack_ts,
        slack_channel_id=(result or {}).get("channel"),
        # Only meaningful for a message that is itself on Slack, and it is the
        # root's *Slack* ts — not the canonical thread_ts, which differ whenever
        # the thread started Slack-off.
        slack_thread_ts=slack_thread_ts if slack_ts else None,
    ))


async def _resolve_other_pi(
    db: AsyncSession, other_agent_id: str,
) -> tuple[AgentRegistry | None, User | None]:
    """Return (AgentRegistry, User) for the other agent's PI, or (reg, None)
    if the agent has no claimed owner yet."""
    reg = (await db.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == other_agent_id)
    )).scalar_one_or_none()
    if not reg or not reg.user_id:
        return reg, None
    user = (await db.execute(select(User).where(User.id == reg.user_id))).scalar_one_or_none()
    return reg, user


async def _slack_enabled_for_migration(
    db: AsyncSession, creator_agent_id: str, other_agent_id: str,
) -> bool:
    """Resolve whether the migration should use Slack.

    Explicit SLACK_ENABLED wins; otherwise auto-detect: Slack is used only when
    both participating bots have usable tokens. See specs/local-db-conversations.md.
    """
    settings = get_settings()
    if settings.slack_enabled is not None:
        return settings.slack_enabled
    from src.services.slack_tokens import get_agent_bot_token
    creator_tok = await get_agent_bot_token(db, creator_agent_id)
    other_tok = await get_agent_bot_token(db, other_agent_id)
    return bool(creator_tok and other_tok)


async def _migrate_offline(
    db: AsyncSession,
    *,
    thread_decision: ThreadDecision,
    creator_agent_id: str,
    creator_pi_user: User,
    guidance_text: str,
    a: str,
    b: str,
    other_agent_id: str,
    origin_channel_name: str,
) -> MigrationResult:
    """Slack-off migration: DB-only, no Slack calls.

    Creates the collab_private AgentChannel and members exactly as the Slack
    path, but with a local: channel id, and writes the handover posts and the
    origin-thread ⏸️ close marker as agent_messages rows so the running sim (and
    the next rebuild) pick them up through _poll_inbound_from_db.
    """
    base_slug = _build_slug(a, b, origin_channel_name)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    new_channel_name = normalize_channel_name(f"{base_slug[: 80 - len(stamp) - 1]}-{stamp}")
    new_channel_id = f"local:{new_channel_name}"
    origin_channel_id = f"local:{origin_channel_name}"

    simulation_run_id = await _latest_simulation_run_id(db)

    ac = AgentChannel(
        simulation_run_id=simulation_run_id,
        channel_id=new_channel_id,
        channel_name=new_channel_name,
        channel_type="collaboration",
        visibility=VISIBILITY_COLLAB_PRIVATE,
        created_by_agent=creator_agent_id,
        migrated_from_channel_id=origin_channel_id,
    )
    db.add(ac)
    await db.flush()

    db.add(PrivateChannelMember(agent_channel_id=ac.id, agent_id=creator_agent_id, role="bot"))
    db.add(PrivateChannelMember(agent_channel_id=ac.id, agent_id=other_agent_id, role="bot"))
    db.add(PrivateChannelMember(
        agent_channel_id=ac.id, user_id=creator_pi_user.id, role="pi",
        added_by_user_id=creator_pi_user.id,
    ))

    # Handover posts, authored by the creator bot, written straight to the DB
    # (visibility=collab_private) so they stay within the channel's membership.
    handover_posts = _build_handover_messages(
        creator_pi_name=creator_pi_user.name,
        proposal_summary=thread_decision.summary_text,
        guidance_text=guidance_text,
        origin_channel_name=origin_channel_name,
    )
    # No Slack post to mirror, so _add_handover_message mints each canonical id
    # from the process-wide minter (never a hand-rolled f"{time.time():.6f}": that
    # carries no writer slot, so it can collide with an id minted by the engine or
    # another writer process, and it round-trips microseconds through a float,
    # which cannot hold them at current epoch magnitudes — see src/agent/ids.py).
    for post in handover_posts:
        _add_handover_message(
            db, simulation_run_id=simulation_run_id, agent_id=creator_agent_id,
            channel_id=new_channel_id, channel_name=new_channel_name,
            content=post, visibility=VISIBILITY_COLLAB_PRIVATE,
        )
    # Neutral close marker in the origin (public) thread — no PI text echoed.
    _add_handover_message(
        db, simulation_run_id=simulation_run_id, agent_id=creator_agent_id,
        channel_id=origin_channel_id, channel_name=origin_channel_name,
        content=_CLOSE_MARKER_TEXT, visibility=VISIBILITY_PUBLIC,
        thread_ts=thread_decision.thread_id,
    )

    thread_decision.refined_in_channel = new_channel_id
    logger.info("Slack-off migration: created private channel %s (DB-only)", new_channel_name)
    return MigrationResult(
        channel_id=new_channel_id,
        channel_name=new_channel_name,
        agent_channel_id=ac.id,
        invited_other_pi=False,
    )


async def migrate_public_thread_to_private(
    db: AsyncSession,
    *,
    thread_decision: ThreadDecision,
    creator_agent_id: str,  # triggering PI's agent — becomes channel creator
    creator_pi_user: User,
    guidance_text: str,
) -> MigrationResult:
    """Create a collab_private channel for this thread and close the public origin.

    Raises on unrecoverable Slack or DB failures. Partial failures (e.g., the
    other PI's DM fails) are logged but do not abort the migration — the
    private channel is the primary artifact.

    Does NOT write the ProposalReview row — the caller (reopen endpoint) owns
    that decision and persists it after this function returns.
    """
    # Identify the other agent in the thread
    a = thread_decision.agent_a
    b = thread_decision.agent_b
    if creator_agent_id not in (a, b):
        raise ValueError(
            f"creator_agent_id '{creator_agent_id}' is not a participant in thread_decision"
        )
    other_agent_id = b if creator_agent_id == a else a

    origin_channel_name = thread_decision.channel

    # Slack-off: DB-only migration (no channel/invite/post/DM). The handover is
    # written straight to agent_messages for the sim to ingest.
    if not await _slack_enabled_for_migration(db, creator_agent_id, other_agent_id):
        return await _migrate_offline(
            db,
            thread_decision=thread_decision,
            creator_agent_id=creator_agent_id,
            creator_pi_user=creator_pi_user,
            guidance_text=guidance_text,
            a=a, b=b,
            other_agent_id=other_agent_id,
            origin_channel_name=origin_channel_name,
        )

    # Resolved up front, before any Slack side-effect: the handover messages are
    # recorded against this run, and failing here *after* creating the Slack
    # channel would leave an orphan channel behind.
    simulation_run_id = await _latest_simulation_run_id(db)

    # --- Slack side-effects ------------------------------------------------
    creator_token = await _get_or_fail_bot_token(db, creator_agent_id)
    other_token = await _get_or_fail_bot_token(db, other_agent_id)
    creator_client = _make_client(creator_agent_id, creator_token)
    other_client = _make_client(other_agent_id, other_token)

    other_bot_user_id = other_client.bot_user_id
    if not other_bot_user_id:
        raise RuntimeError(f"Could not resolve bot user ID for '{other_agent_id}'")

    slug = _build_slug(a, b, origin_channel_name)
    new_channel = creator_client.create_private_channel(slug)
    if not new_channel:
        raise RuntimeError(f"Slack refused to create private channel '{slug}'")
    new_channel_id = new_channel["id"]
    new_channel_name = new_channel["name"]

    # Invite: the other bot + the triggering PI. The other PI (if exists)
    # is handled separately via a DM below — inviting them to the channel
    # directly would silently add them without context, which we don't want.
    invitees = [other_bot_user_id]
    creator_pi_slack_id = (await db.execute(
        select(AgentRegistry.slack_user_id).where(AgentRegistry.agent_id == creator_agent_id)
    )).scalar_one_or_none()
    if creator_pi_slack_id:
        invitees.append(creator_pi_slack_id)
    if not creator_client.invite_to_channel(new_channel_id, invitees):
        logger.warning(
            "Some invites to %s failed — channel exists but membership may be incomplete",
            new_channel_id,
        )

    # Resolve origin channel ID: we need it to close the origin thread.
    # creator_client caches channel IDs from any earlier lookups, but the
    # web app is short-lived, so just look it up fresh.
    origin_channel_id = creator_client._resolve_channel_id(origin_channel_name)

    # Post the handover as 2+ top-level messages so each stays within
    # Slack's per-message length limit and no content gets orphaned in a
    # mid-bullet split. All posts go top-level — collab_private channels
    # are flat (no threading).
    handover_posts = _build_handover_messages(
        creator_pi_name=creator_pi_user.name,
        proposal_summary=thread_decision.summary_text,
        guidance_text=guidance_text,
        origin_channel_name=origin_channel_name,
    )
    # Each Slack result is kept so the DB rows below can carry the canonical id
    # Slack assigned (and the mirror mapping). The DB is the primary conversation
    # store — a handover that existed only on Slack would be invisible to a
    # Slack-off restart and to the web conversation view.
    handover_results: list[tuple[str, dict | None]] = [
        (post, creator_client.post_message(new_channel_id, post))
        for post in handover_posts
    ]

    # Close the origin thread with a neutral marker — NO PI text echoed. Slack
    # threads on the root's *Slack* ts, which equals the canonical thread id only
    # when the root was born on Slack, so translate first and keep the marker
    # DB-only when the thread has no Slack presence.
    slack_parent = await _slack_parent_ts_from_db(
        db, simulation_run_id, thread_decision.thread_id,
    )
    close_result = None
    if slack_parent is None:
        logger.warning(
            "Not mirroring the close marker for thread %s: its root has no Slack "
            "presence (started with Slack off). The marker is still recorded in the DB.",
            thread_decision.thread_id,
        )
    else:
        try:
            close_result = creator_client.post_message(
                origin_channel_id, _CLOSE_MARKER_TEXT, thread_ts=slack_parent,
            )
        except ThreadNotFound:
            # Origin root was deleted on Slack. post_message already cleaned up the
            # orphan top-level post; the DB marker below still records the close, so
            # the migration completes rather than aborting a PI-initiated action.
            logger.warning(
                "Origin thread %s no longer exists on Slack — close marker recorded "
                "in the DB only", thread_decision.thread_id,
            )

    # Invite the other PI via DM from their own bot. Best-effort — if this
    # fails (no claimed PI, no Slack ID, DM not allowed), refinement still
    # proceeds.
    invited_other_pi = False
    other_reg, other_pi = await _resolve_other_pi(db, other_agent_id)
    if other_pi and other_reg and other_reg.slack_user_id:
        try:
            # Also invite them to the channel first (so when they click the
            # link they can see the history). Tolerant of already_in_channel.
            other_client.invite_to_channel(new_channel_id, [other_reg.slack_user_id])
            dm_text = _build_other_pi_dm(
                other_pi_name=other_pi.name,
                creator_pi_name=creator_pi_user.name,
                origin_channel_name=origin_channel_name,
                new_channel_name=new_channel_name,
            )
            other_client.send_dm(other_reg.slack_user_id, dm_text)
            invited_other_pi = True
        except Exception as exc:
            logger.warning(
                "Failed to notify %s's PI of migration: %s", other_agent_id, exc,
            )

    # --- DB writes ---------------------------------------------------------
    ac = AgentChannel(
        simulation_run_id=simulation_run_id,
        channel_id=new_channel_id,
        channel_name=new_channel_name,
        channel_type="collaboration",  # legacy enum — see data-model.md
        visibility=VISIBILITY_COLLAB_PRIVATE,
        created_by_agent=creator_agent_id,
        migrated_from_channel_id=origin_channel_id,
    )
    db.add(ac)
    await db.flush()  # populate ac.id for member FKs

    # Bot members
    db.add(PrivateChannelMember(
        agent_channel_id=ac.id, agent_id=creator_agent_id, role="bot",
    ))
    db.add(PrivateChannelMember(
        agent_channel_id=ac.id, agent_id=other_agent_id, role="bot",
    ))
    # Triggering PI
    db.add(PrivateChannelMember(
        agent_channel_id=ac.id,
        user_id=creator_pi_user.id,
        role="pi",
        added_by_user_id=creator_pi_user.id,
    ))
    # The other PI is deliberately not added as a member here — they only
    # become a member when they accept the Slack invite. No DB write until then.

    # Mirror the handover into agent_messages. Same rows as the Slack-off path,
    # additionally carrying the slack_* mapping, so the running simulation picks
    # them up via _poll_inbound_from_db and a rebuild reconstructs the channel
    # from the DB alone rather than depending on Slack history.
    for post, result in handover_results:
        _add_handover_message(
            db, simulation_run_id=simulation_run_id, agent_id=creator_agent_id,
            channel_id=new_channel_id, channel_name=new_channel_name,
            content=post, visibility=VISIBILITY_COLLAB_PRIVATE, result=result,
        )
    _add_handover_message(
        db, simulation_run_id=simulation_run_id, agent_id=creator_agent_id,
        channel_id=origin_channel_id, channel_name=origin_channel_name,
        content=_CLOSE_MARKER_TEXT, visibility=VISIBILITY_PUBLIC,
        result=close_result, thread_ts=thread_decision.thread_id,
        slack_thread_ts=slack_parent,
    )

    # Record the refinement destination on the thread_decision
    thread_decision.refined_in_channel = new_channel_id

    return MigrationResult(
        channel_id=new_channel_id,
        channel_name=new_channel_name,
        agent_channel_id=ac.id,
        invited_other_pi=invited_other_pi,
    )
