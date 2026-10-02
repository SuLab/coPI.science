"""My Agent page router."""

import logging
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import distinct, func, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.config import get_settings
from src.database import get_db
from src.dependencies import (
    get_agent_with_access,
    get_current_user,
    get_pi_user,
    impersonation_note,
)
from src.models import (
    AgentDelegate,
    AgentMessage,
    AgentRegistry,
    DelegateInvitation,
    ResearcherProfile,
    User,
)
from src.services.agent_identity import derive_agent_identity
from src.services.conversation_feed import own_or_gated, resolve_agent_gate
from src.services.email import build_delegate_invitation, send_transactional_email
from src.services.profile_edit import (
    apply_profile_edits,
    list_fields_from_form,
    parse_expected_version,
)
from src.services.runs import latest_run_id
from src.services.validators import is_valid_email
from src.web.flash import flash
from src.web.templating import make_templates

logger = logging.getLogger(__name__)
router = APIRouter()
templates = make_templates()

SLACK_INVITE_URL = (
    "https://join.slack.com/t/labbot-workspace/shared_invite/"
    "zt-3sxfrrisw-t4hRz4aMfZZPxThxUaTGKA"
)

# Thread roots per page. The window's unit is threads, not messages: replies no
# longer consume slots, so this surfaces more distinct conversations than the
# previous flat 100-message window did.
_ROOT_LIMIT = 50

#: Delegate-invite caps (SN-02): per submission, and per agent over any rolling
#: 24 hours. The window counts every delegate_invitations row created in it,
#: whatever its status, so revoke-and-resend cannot get around it; the rows are
#: the ledger, no table of their own.
_INVITES_PER_SUBMISSION = 10
_INVITES_PER_AGENT_PER_DAY = 25


async def _visible_channels(db: AsyncSession, run_id, aid: str) -> list[str]:
    """Channels this agent participates in (has authored a message in), plus
    #general.

    Shared by ``agent_conversations`` and ``agent_thread_replies`` — the
    channel set is one of the thread-expand endpoint's four authorization
    axes (``channel_name.in_(channels)`` on the root re-resolution query), not
    just a display filter, so it must be computed identically in both places
    rather than copy-pasted and left free to drift.
    """
    ch_rows = await db.execute(
        select(distinct(AgentMessage.channel_name)).where(
            AgentMessage.simulation_run_id == run_id,
            AgentMessage.agent_id == aid,
        )
    )
    return sorted({r[0] for r in ch_rows} | {"general"})


def _template_context(request: Request, user: User, **kwargs) -> dict:
    impersonated = getattr(user, "_is_impersonated", False)
    real_admin = getattr(user, "_real_admin", None)
    ctx = {
        "request": request,
        "current_user": real_admin if impersonated else user,
        "user": user,
        "impersonation_banner": user if impersonated else None,
        "active_page": "agent",
    }
    ctx.update(kwargs)
    return ctx


# --------------------------------------------------------------------------
# Landing page — agent listing / auto-redirect
# --------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def agent_landing(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Agent landing page — lists all agents the user has access to."""
    # Own agent
    result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == current_user.id)
    )
    own_agent = result.scalar_one_or_none()

    # Delegated agents
    delegated_result = await db.execute(
        select(AgentRegistry)
        .join(AgentDelegate, AgentDelegate.agent_registry_id == AgentRegistry.id)
        .where(AgentDelegate.user_id == current_user.id)
    )
    delegated_agents = delegated_result.scalars().all()

    # Collect all accessible agents
    all_agents = []
    if own_agent:
        all_agents.append(own_agent)
    all_agents.extend(delegated_agents)

    # Auto-redirect if exactly one agent and it can reach the dashboard.
    # Inactive agents are included: their owner can still see the read-only
    # dashboard (the dashboard template gates the active-only settings).
    if len(all_agents) == 1 and all_agents[0].status in ("active", "inactive"):
        return RedirectResponse(
            url=f"/agent/{all_agents[0].agent_id}/dashboard", status_code=302
        )

    # No agents at all — show request page
    if not all_agents:
        has_profile = (
            current_user.onboarding_complete
            and current_user.profile
            and current_user.profile.research_summary
        )
        return templates.TemplateResponse(
            request,
            "agent/request.html",
            _template_context(
                request, current_user, agent=None, has_profile=has_profile
            ),
        )

    # Single agent but pending — show request page
    if len(all_agents) == 1 and own_agent and own_agent.status == "pending":
        return templates.TemplateResponse(
            request,
            "agent/request.html",
            _template_context(request, current_user, agent=own_agent),
        )

    # Multiple agents (or single delegated) — show listing
    return templates.TemplateResponse(
        request,
        "agent/listing.html",
        _template_context(
            request,
            current_user,
            own_agent=own_agent,
            delegated_agents=delegated_agents,
        ),
    )


# --------------------------------------------------------------------------
# Agent dashboard
# --------------------------------------------------------------------------


@router.get("/{agent_id}/dashboard", response_class=HTMLResponse)
async def agent_dashboard(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Agent dashboard — stats, delegates and settings.

    Inactive agents are allowed in (read-only): they are parked from
    simulation runs but their owner can still see the dashboard.
    ``pending``/``suspended`` agents stay gated out. The active-only settings
    are gated in the template.
    """
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)

    if agent.status not in ("active", "inactive"):
        return RedirectResponse(url="/agent", status_code=302)

    aid = agent.agent_id

    # Stats
    posts_count_result = await db.execute(
        select(func.count(AgentMessage.id)).where(
            AgentMessage.agent_id == aid,
            AgentMessage.phase == "new_post",
        )
    )
    posts_count = posts_count_result.scalar() or 0

    threads_count_result = await db.execute(
        select(func.count(distinct(AgentMessage.thread_ts))).where(
            AgentMessage.agent_id == aid,
            AgentMessage.phase == "thread_reply",
        )
    )
    threads_count = threads_count_result.scalar() or 0

    # Pending invitations (for PI view)
    pending_invitations = []
    if is_owner:
        pending_result = await db.execute(
            select(DelegateInvitation).where(
                DelegateInvitation.agent_registry_id == agent.id,
                DelegateInvitation.status == "pending",
            ).order_by(DelegateInvitation.created_at.desc())
        )
        pending_invitations = pending_result.scalars().all()

    # Web delegates
    web_delegates_result = await db.execute(
        select(AgentDelegate)
        .options(selectinload(AgentDelegate.user))
        .where(AgentDelegate.agent_registry_id == agent.id)
    )
    web_delegates = web_delegates_result.scalars().all()

    return templates.TemplateResponse(
        request,
        "agent/dashboard.html",
        _template_context(
            request,
            current_user,
            agent=agent,
            is_owner=is_owner,
            posts_count=posts_count,
            threads_count=threads_count,
            slack_invite_url=SLACK_INVITE_URL,
            web_delegates=web_delegates,
            pending_invitations=pending_invitations,
        ),
    )


# --------------------------------------------------------------------------
# Request an agent
# --------------------------------------------------------------------------


@router.post("/request")
async def request_agent(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_pi_user),
):
    """Submit an agent request.

    The dependency, not the body check, is what keeps managers out. The
    onboarding_complete/profile test below is a readiness check, not an
    authorization one: a manager who acquired both (by any route, now or
    later) would otherwise walk straight through it and receive an
    AgentRegistry row — a lab of its own, which D7 forbids.
    """
    if not current_user.onboarding_complete or not current_user.profile:
        raise HTTPException(status_code=400, detail="Complete your profile first")

    existing = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == current_user.id)
    )
    if existing.scalar_one_or_none():
        return RedirectResponse(url="/agent", status_code=302)

    agent_id, bot_name = await derive_agent_identity(
        db, current_user.name, orcid=current_user.orcid
    )

    agent = AgentRegistry(
        agent_id=agent_id,
        user_id=current_user.id,
        bot_name=bot_name,
        pi_name=current_user.name,
        status="pending",
    )
    user_id = current_user.id  # read before a rollback expires the instance
    db.add(agent)
    try:
        await db.commit()
    except IntegrityError:
        # A double submit lost the race on agents.user_id (the first request created
        # the lab); anything else, such as an agent_id collision with another user,
        # is a real error and propagates (RB-11).
        await db.rollback()
        mine = await db.execute(select(AgentRegistry.id).where(AgentRegistry.user_id == user_id))
        if mine.scalar_one_or_none() is None:
            raise
        return RedirectResponse(url="/agent", status_code=302)
    logger.info(
        "agent %s: requested by %s (%s)",
        agent_id, current_user.id, impersonation_note(current_user) or "direct",
    )

    return RedirectResponse(url="/agent", status_code=302)


# --------------------------------------------------------------------------
# Conversations (DB-inbox messaging; Slack-independent)
# --------------------------------------------------------------------------


@router.get("/{agent_id}/conversations", response_class=HTMLResponse)
async def agent_conversations(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Read-only view of the agent's recent conversations.

    There is no write path here: the 2026-08-12 PI-interaction removal cycle
    deleted the web posting form (``post_agent_message``) along with every
    other human-PI-to-bot interaction surface. This is now purely a
    Slack-independent window onto what the agent's workspace is discussing.
    See specs/local-db-conversations.md.
    """
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    if agent.status not in ("active", "inactive"):
        return RedirectResponse(url="/agent", status_code=302)
    aid = agent.agent_id

    run_id = await latest_run_id(db)
    channels: list[str] = []
    messages: list[dict] = []
    if run_id:
        channels = await _visible_channels(db, run_id, aid)
        # What this PI may read == what their bot may act on. Filtering happens in
        # SQL, before LIMIT: #general carries every other cohort's traffic, so
        # filtering in Python afterwards would leave the page nearly empty.
        gate = await resolve_agent_gate(db, aid)
        # own_or_gated (src/services/conversation_feed.py) is gate_clause widened
        # with the PI's own-post carve-out — see its docstring for why the OR is
        # needed. One expression here, in the reply-count query below, and in
        # agent_thread_replies keeps the feed, the badge, and the expansion from
        # ever disagreeing on what a PI may see.
        gated = own_or_gated(gate, aid)

        # Thread ROOTS, newest first. `phase` is belt-and-braces alongside
        # `thread_ts IS NULL`; the two agree on every row.
        #
        # The three-column ordering is load-bearing, not stylistic. Migration
        # 0019 adds posted_at with server_default '0', so EVERY row that
        # predates it shares one value. With `ORDER BY posted_at DESC LIMIT
        # 50` over a tie group larger than 50, Postgres is free to return any
        # 50 — measured on a 200-row tie group, the index-scan and seq-scan
        # plans returned two DISJOINT pages, so half the messages were
        # unreachable and which half flipped with the plan. Adding created_at
        # and the primary key makes the sort total, so the page is stable and
        # every row is reachable by paging.
        root_rows = await db.execute(
            select(AgentMessage)
            .where(
                AgentMessage.simulation_run_id == run_id,
                AgentMessage.channel_name.in_(channels),
                AgentMessage.thread_ts.is_(None),
                AgentMessage.phase == "new_post",
                gated,
            )
            .order_by(AgentMessage.posted_at.desc(), AgentMessage.created_at.desc(),
                      AgentMessage.id.desc())
            .limit(_ROOT_LIMIT)
        )
        roots = list(reversed(root_rows.scalars().all()))

        # Reply counts, gated with the SAME clause (including the own-post
        # carve-out) so the badge can never promise turns the expansion will not
        # show. The real invariant a reply query must honour is that a reply
        # lives in ITS ROOT's channel — `uq_agent_messages_run_ts`
        # (src/models/agent_activity.py) only proves root ids don't collide
        # across channels within a run; it says nothing about where a reply
        # naming that root as `thread_ts` was posted. Nothing else enforces that
        # a `collab_private` reply's `thread_ts` can't coincide with a public
        # root's `message_ts` — and `gate_clause`'s unconditional
        # `collab_private` pass would let such a reply count toward (and, via
        # the expand endpoint, render into) a conversation it does not belong
        # to. So this matches `(thread_ts, channel_name)` pairs against each
        # root's own channel, not `thread_ts` alone.
        root_pairs = [(r.message_ts, r.channel_name) for r in roots if r.message_ts]
        counts: dict[str, int] = {}
        if root_pairs:
            count_rows = await db.execute(
                select(AgentMessage.thread_ts, func.count(AgentMessage.id))
                .where(
                    AgentMessage.simulation_run_id == run_id,
                    tuple_(AgentMessage.thread_ts, AgentMessage.channel_name).in_(root_pairs),
                    gated,
                )
                .group_by(AgentMessage.thread_ts)
            )
            counts = {ts: n for ts, n in count_rows}

        messages = [
            {
                "channel": m.channel_name,
                "sender": m.sender_name or (m.agent_id or "PI"),
                "is_bot": m.is_bot,
                "content": m.content,
                "message_ts": m.message_ts,
                "thread_ts": m.thread_ts,
                "reply_count": counts.get(m.message_ts, 0),
                "posted_at": m.posted_at,
            }
            for m in roots
        ]
    else:
        channels = ["general"]

    return templates.TemplateResponse(
        request,
        "agent/conversations.html",
        _template_context(
            request, current_user, agent=agent, is_owner=is_owner,
            messages=messages, has_run=run_id is not None,
        ),
    )


@router.get("/{agent_id}/thread/{message_ts}", response_class=HTMLResponse)
async def agent_thread_replies(
    agent_id: str,
    message_ts: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Replies for one thread, as an HTML fragment for the conversations page.

    ``message_ts`` is a guessable identifier, so authorisation cannot stop at the
    agent: the ROOT is re-resolved under this agent's channel set and cohort gate
    before any reply is read. Anything that does not resolve is a 404 — absent,
    not-a-root, another channel, and out-of-cohort are deliberately
    indistinguishable to the caller.

    Replies are gated too, with the same clause that produced the count on the
    page (``own_or_gated``), so the badge and the expansion can never disagree.
    This diverges from the engine, which classifies ``get_thread_history`` as
    UNGATED (``src/agent/message_log.py:407-408``) because it is thread-internal;
    here the whole point is that out-of-cohort traffic must not become reachable
    by clicking, and a future reader should not "fix" this back toward engine
    parity.
    """
    agent, _is_owner = await get_agent_with_access(agent_id, db, current_user)
    if agent.status not in ("active", "inactive"):
        raise HTTPException(status_code=404)
    aid = agent.agent_id

    run_id = await latest_run_id(db)
    if not run_id:
        raise HTTPException(status_code=404)

    channels = await _visible_channels(db, run_id, aid)

    gate = await resolve_agent_gate(db, aid)
    gated = own_or_gated(gate, aid)
    root = (await db.execute(
        select(AgentMessage)
        .where(
            AgentMessage.simulation_run_id == run_id,
            AgentMessage.message_ts == message_ts,
            AgentMessage.thread_ts.is_(None),
            AgentMessage.phase == "new_post",
            AgentMessage.channel_name.in_(channels),
            gated,
        )
        .limit(1)
    )).scalar_one_or_none()
    if root is None:
        raise HTTPException(status_code=404)

    # Scoped to the root's OWN channel, not just `thread_ts` — see the count
    # query's comment in `agent_conversations` for why `thread_ts` alone is not
    # the invariant a reply query can rely on.
    reply_rows = await db.execute(
        select(AgentMessage)
        .where(
            AgentMessage.simulation_run_id == run_id,
            AgentMessage.thread_ts == message_ts,
            AgentMessage.channel_name == root.channel_name,
            gated,
        )
        .order_by(AgentMessage.posted_at.asc(), AgentMessage.created_at.asc(),
                  AgentMessage.id.asc())
    )
    replies = [
        {
            "sender": m.sender_name or (m.agent_id or "PI"),
            "is_bot": m.is_bot,
            "content": m.content,
        }
        for m in reply_rows.scalars().all()
    ]

    return templates.TemplateResponse(
        request, "agent/_thread_replies.html", {"replies": replies}
    )


# --------------------------------------------------------------------------
# Public profile view/edit (PI and delegates)
# --------------------------------------------------------------------------


@router.get("/{agent_id}/public-profile", response_class=HTMLResponse)
async def view_public_profile(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """View agent's public profile."""
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    if agent.status != "active":
        return RedirectResponse(url="/agent", status_code=302)

    if agent.user_id is None:
        # Orphaned agent — its PI account was deleted before the teardown
        # service existed (deletion audit F9). There is no PI profile here.
        return RedirectResponse(url="/agent", status_code=302)

    # Load the PI's profile (not the delegate's)
    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == agent.user_id)
    )
    profile = profile_result.scalar_one_or_none()

    # Load PI user for display
    pi_result = await db.execute(select(User).where(User.id == agent.user_id))
    pi_user = pi_result.scalar_one()

    return templates.TemplateResponse(
        request,
        "agent/public_profile.html",
        _template_context(
            request, current_user, agent=agent, is_owner=is_owner,
            profile=profile, pi_user=pi_user, editing=False,
            saved=request.query_params.get("saved"),
        ),
    )


@router.get("/{agent_id}/public-profile/edit", response_class=HTMLResponse)
async def edit_public_profile(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Edit agent's public profile."""
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    if agent.status != "active":
        return RedirectResponse(url="/agent", status_code=302)

    if agent.user_id is None:
        # Orphaned agent — its PI account was deleted before the teardown
        # service existed (deletion audit F9). There is no PI profile here.
        return RedirectResponse(url="/agent", status_code=302)

    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == agent.user_id)
    )
    profile = profile_result.scalar_one_or_none()

    pi_result = await db.execute(select(User).where(User.id == agent.user_id))
    pi_user = pi_result.scalar_one()

    return templates.TemplateResponse(
        request,
        "agent/public_profile.html",
        _template_context(
            request, current_user, agent=agent, is_owner=is_owner,
            profile=profile, pi_user=pi_user, editing=True,
        ),
    )


@router.post("/{agent_id}/public-profile/save")
async def save_public_profile(
    agent_id: str,
    request: Request,
    research_summary: str = Form(""),
    profile_version: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save public profile changes (PI or delegate)."""
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    if agent.status != "active":
        return RedirectResponse(url="/agent", status_code=302)

    if agent.user_id is None:
        # Orphaned agent (deletion audit F9): save would build
        # ResearcherProfile(user_id=None), a NOT NULL column. Refuse loudly;
        # replaying a POST as a redirect would just hide the state.
        raise HTTPException(
            status_code=409,
            detail="This lab is no longer linked to a PI account",
        )

    note = impersonation_note(current_user)
    pi_user = (await db.execute(select(User).where(User.id == agent.user_id))).scalar_one()
    error = await apply_profile_edits(
        db, target_user=pi_user, changed_by_user_id=current_user.id,
        form={
            "research_summary": research_summary,
            **list_fields_from_form(await request.form()),
        },
        expected_version=parse_expected_version(profile_version),
        export_agent=agent,
        change_summary=note,
        mechanism="web_impersonated" if note else "web",
    )
    if error:
        return RedirectResponse(
            url=f"/agent/{agent_id}/public-profile/edit?error={error}", status_code=302,
        )

    logger.info(
        "Public profile for agent %s updated by %s (%s)",
        agent.agent_id, current_user.name, note or "direct",
    )

    return RedirectResponse(
        url=f"/agent/{agent_id}/public-profile?saved=1", status_code=302
    )


# --------------------------------------------------------------------------
# Delegate management — invitation-based
# --------------------------------------------------------------------------


@router.post("/{agent_id}/delegates/invite")
async def invite_delegate(
    agent_id: str,
    request: Request,
    emails: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Send delegate invitation(s) by email.

    The agent row is locked FOR UPDATE for the request, so two concurrent invites
    serialize and the "already pending" check sees the first one's row (there is no
    unique constraint on pending invitations). The emails go out only after the
    commit, so none announces an invitation that then rolls back. At most
    ``_INVITES_PER_SUBMISSION`` addresses per submission (else nothing is created)
    and ``_INVITES_PER_AGENT_PER_DAY`` invitation rows per agent in any 24 hours
    (SN-02).
    """
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    agent = (await db.execute(
        select(AgentRegistry).where(AgentRegistry.id == agent.id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one()
    note = impersonation_note(current_user)
    logger.info("agent %s: delegate invite by %s (%s)", agent.agent_id, current_user.id, note or "direct")
    if not is_owner:
        raise HTTPException(status_code=403, detail="Only the PI can manage delegates")
    if agent.status != "active":
        return RedirectResponse(url="/agent", status_code=302)

    settings = get_settings()

    # Parse comma/newline-separated emails
    email_list = [
        e.strip().lower()
        for e in re.split(r"[,\n]+", emails)
        if e.strip()
    ]
    if len(email_list) > _INVITES_PER_SUBMISSION:
        flash(
            request,
            f"At most {_INVITES_PER_SUBMISSION} addresses per invitation; nothing was sent.",
            "error",
        )
        return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)
    # The agent row is locked FOR UPDATE above, so this count and the inserts
    # below cannot interleave with another invite for the same agent.
    created_last_day = await db.scalar(
        select(func.count(DelegateInvitation.id)).where(
            DelegateInvitation.agent_registry_id == agent.id,
            DelegateInvitation.created_at >= datetime.now(UTC) - timedelta(hours=24),
        )
    ) or 0
    remaining_today = max(0, _INVITES_PER_AGENT_PER_DAY - created_last_day)

    errors = []
    to_send: list[tuple[str, str]] = []
    for email in email_list:
        # Basic validation (length-capped to avoid ReDoS; see SEC-16)
        if not is_valid_email(email):
            errors.append(f"Invalid email: {email}")
            continue

        # Don't invite yourself
        if current_user.email and email == current_user.email.lower():
            errors.append("You can't invite yourself.")
            continue

        # Check if already an active delegate
        existing_delegate = await db.execute(
            select(AgentDelegate)
            .join(User, AgentDelegate.user_id == User.id)
            .where(
                AgentDelegate.agent_registry_id == agent.id,
                func.lower(User.email) == email,
            )
        )
        if existing_delegate.scalar_one_or_none():
            errors.append(f"{email} is already a delegate.")
            continue

        # Check for pending invitation
        existing_invite = await db.execute(
            select(DelegateInvitation).where(
                DelegateInvitation.agent_registry_id == agent.id,
                DelegateInvitation.email == email,
                DelegateInvitation.status == "pending",
            )
        )
        if existing_invite.scalar_one_or_none():
            errors.append(f"Invitation already pending for {email}.")
            continue

        if remaining_today <= 0:
            errors.append(
                f"Daily limit of {_INVITES_PER_AGENT_PER_DAY} invitations per agent "
                f"reached; {email} was not invited."
            )
            continue

        # Create invitation
        token = secrets.token_urlsafe(48)
        invitation = DelegateInvitation(
            agent_registry_id=agent.id,
            invited_by_user_id=current_user.id,
            email=email,
            token=token,
            status="pending",
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        db.add(invitation)
        await db.flush()  # Get the ID
        remaining_today -= 1

        to_send.append((email, f"{settings.base_url}/invite/{token}"))

    await db.commit()

    # The invitation exists regardless of whether the email gets through; the PI
    # is told which ones did not go out (D-21). send_transactional_email returns
    # False when the outbound allowlist suppresses the address or SES fails.
    unsent: list[str] = []
    for email, invite_url in to_send:
        sent = await send_transactional_email(
            build_delegate_invitation(email, agent.pi_name, agent.bot_name, invite_url)
        )
        if not sent:
            unsent.append(email)

    if errors:
        flash(request, "; ".join(errors), "error")
    if unsent:
        # A count plus the first three addresses, as its own flash: flash text is
        # capped at MAX_FLASH_CHARS, so a full list, or one appended after the
        # other errors, could be cut off (plan audit Q2-12).
        shown = ", ".join(unsent[:3]) + (f" and {len(unsent) - 3} more" if len(unsent) > 3 else "")
        flash(
            request,
            f"Invitation saved, but no email was sent to {len(unsent)} address(es): {shown}.",
            "error",
        )
    return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)


@router.post("/{agent_id}/delegates/{invitation_id}/revoke")
async def revoke_invitation(
    agent_id: str,
    invitation_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Revoke a pending delegate invitation."""
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    note = impersonation_note(current_user)
    logger.info("agent %s: invitation revoke by %s (%s)", agent.agent_id, current_user.id, note or "direct")
    if not is_owner:
        raise HTTPException(status_code=403, detail="Only the PI can manage delegates")

    result = await db.execute(
        select(DelegateInvitation).where(
            DelegateInvitation.id == invitation_id,
            DelegateInvitation.agent_registry_id == agent.id,
            DelegateInvitation.status == "pending",
        )
    )
    invitation = result.scalar_one_or_none()
    if invitation:
        invitation.status = "revoked"
        await db.commit()

    return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)


@router.post("/{agent_id}/delegates/{delegate_id}/remove")
async def remove_delegate(
    agent_id: str,
    delegate_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove an active delegate."""
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    note = impersonation_note(current_user)
    if not is_owner:
        raise HTTPException(status_code=403, detail="Only the PI can manage delegates")

    result = await db.execute(
        select(AgentDelegate)
        .options(selectinload(AgentDelegate.user))
        .where(
            AgentDelegate.id == delegate_id,
            AgentDelegate.agent_registry_id == agent.id,
        )
    )
    delegate = result.scalar_one_or_none()
    if delegate:
        await db.delete(delegate)
        await db.commit()
        logger.info(
            "Delegate %s removed from agent %s by %s (%s)",
            delegate.user_id, agent.agent_id, current_user.name, note or "direct",
        )

    return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)
