"""My Agent page router."""

import asyncio
import logging
import re
import uuid
from datetime import UTC
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import distinct, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.database import get_db
from src.dependencies import get_agent_with_access, get_current_user
from src.models import (
    AgentDelegate,
    AgentMessage,
    AgentRegistry,
    LlmCallLog,
    ProposalReview,
    ResearcherProfile,
    ThreadDecision,
    User,
)
from src.services.profile_export import export_profile_to_markdown
from src.services.validators import is_valid_email

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")

PROFILES_DIR = Path("profiles")
SLACK_INVITE_URL = (
    "https://join.slack.com/t/labbot-workspace/shared_invite/"
    "zt-3sxfrrisw-t4hRz4aMfZZPxThxUaTGKA"
)


def _extract_proposal_title(text: str | None) -> str:
    """Best-effort one-line title for a proposal summary.

    Proposal summaries open with a ":memo: Summary" header in many shapes
    (``:memo: Summary``, ``:memo: **Summary**``, ``:memo: **Summary — Foo**``,
    ``Summary:`` …). Strip that boilerplate so the dashboard shows the real
    subject — the title following a ``Summary —`` separator, or the first real
    content line — instead of the literal ":memo: Summary".
    """
    if not text:
        return "Collaboration Proposal"
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # Drop a leading :memo: / 📝 marker, a markdown heading prefix, and
        # surrounding bold markers.
        line = re.sub(r"^\s*(?::memo:|📝)\s*", "", line)
        line = re.sub(r"^#+\s*", "", line)
        line = line.replace("**", "").strip()
        if not line:
            continue
        # If this is the "Summary" header, use any title that follows a
        # separator on the same line; otherwise treat the header as noise and
        # keep scanning for the first real content line.
        m = re.match(r"(?i)^summary\b\s*[—–\-:+.]*\s*(.*)$", line)
        if m:
            rest = m.group(1).strip()
            if rest and re.search(r"\w", rest):
                return rest[:120]
            continue
        return line[:120]
    return "Collaboration Proposal"


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
    # Inactive agents are included: their owner can still review existing
    # proposals (the dashboard itself gates reopen + active-only settings).
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
    """Agent dashboard — shows stats, proposals, and settings.

    Inactive agents are allowed in (read + rate existing proposals only): they
    are parked from simulation runs but their owner should still be able to
    review proposals generated before inactivation. ``pending``/``suspended``
    agents stay gated out. The reopen action and the active-only settings are
    gated separately (see ``reopen_proposal`` and the dashboard template).
    """
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)

    if agent.status not in ("active", "inactive"):
        return RedirectResponse(url="/agent", status_code=302)

    aid = agent.agent_id
    slack_error = request.query_params.get("slack_error")

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

    # Proposals where this agent is involved
    proposals_result = await db.execute(
        select(ThreadDecision)
        .where(
            ThreadDecision.outcome == "proposal",
            (ThreadDecision.agent_a == aid) | (ThreadDecision.agent_b == aid),
        )
        .order_by(ThreadDecision.decided_at.desc())
    )
    proposals = proposals_result.scalars().all()

    # Get existing reviews by this agent
    reviewed_ids_result = await db.execute(
        select(ProposalReview.thread_decision_id).where(
            ProposalReview.agent_id == aid
        )
    )
    reviewed_ids = {r[0] for r in reviewed_ids_result}

    # Separate into reviewed and unreviewed
    unreviewed = []
    reviewed = []
    for p in proposals:
        other = p.agent_b if p.agent_a == aid else p.agent_a
        title = _extract_proposal_title(p.summary_text)
        entry = {"proposal": p, "other_agent": other, "title": title}
        if p.id in reviewed_ids:
            rev_result = await db.execute(
                select(ProposalReview).where(
                    ProposalReview.thread_decision_id == p.id,
                    ProposalReview.agent_id == aid,
                )
            )
            entry["review"] = rev_result.scalar_one_or_none()
            reviewed.append(entry)
        else:
            # Fetch discussion: one entry per actual Slack message in this thread.
            # LlmCallLog logs every API call; tool-use chains produce multiple entries
            # per turn with empty/partial response_text. Fix: filter blanks, then
            # collapse consecutive same-agent entries (keep the last/fullest one).
            disc_result = await db.execute(
                select(LlmCallLog.agent_id, LlmCallLog.response_text, LlmCallLog.created_at)
                .where(
                    LlmCallLog.channel == p.channel,
                    LlmCallLog.phase == "thread_reply",
                    LlmCallLog.agent_id.in_([p.agent_a, p.agent_b]),
                    LlmCallLog.created_at <= p.decided_at,
                    func.length(LlmCallLog.response_text) > 10,
                )
                .order_by(LlmCallLog.created_at.asc())
            )
            raw_msgs = [
                {
                    "agent_id": r[0],
                    "text": re.sub(r"</?slack_message>", "", r[1]).strip(),
                    "ts": r[2].isoformat(),
                }
                for r in disc_result
                if r[1] and r[1].strip()
            ]
            deduped: list[dict] = []
            for msg in raw_msgs:
                if deduped and deduped[-1]["agent_id"] == msg["agent_id"]:
                    deduped[-1] = msg
                else:
                    deduped.append(msg)
            entry["discussion"] = deduped
            unreviewed.append(entry)

    # Private profile path
    private_profile_path = PROFILES_DIR / "private" / f"{aid}.md"
    has_private_profile = private_profile_path.exists()

    # Resolve delegate display names (legacy Slack-only delegates)
    delegates = []
    if agent.delegate_slack_ids:
        from src.services.slack_tokens import get_any_bot_token
        # to_thread because _resolve_delegate_names is sync and calls
        # slack_web.get_user_info once per delegate, each of which can retry with
        # backoff. Run inline it would block the event loop for every other
        # request the process is serving, not just this dashboard render.
        delegates = await asyncio.to_thread(
            _resolve_delegate_names,
            agent.delegate_slack_ids, await get_any_bot_token(db),
        )

    # Pending invitations (for PI view)
    from src.models import DelegateInvitation
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

    # Check if current delegate user has Slack linked
    delegate_has_slack = True
    if not is_owner:
        delegate_slack_ids = agent.delegate_slack_ids or []
        # Check if any of the delegate's possible Slack IDs are in the list
        # For now, we check by trying to find their user in the web delegates
        delegate_has_slack = any(
            _user_slack_id_in_list(wd.user, delegate_slack_ids)
            for wd in web_delegates
            if wd.user_id == current_user.id
        )

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
            proposals_total=len(proposals),
            unreviewed=unreviewed,
            reviewed=reviewed,
            has_private_profile=has_private_profile,
            slack_invite_url=SLACK_INVITE_URL,
            slack_error=slack_error,
            delegates=delegates,
            web_delegates=web_delegates,
            pending_invitations=pending_invitations,
            delegate_has_slack=delegate_has_slack,
            delegate_error=request.query_params.get("delegate_error"),
        ),
    )


def _user_slack_id_in_list(user: User, slack_ids: list[str]) -> bool:
    """Check if a user's email maps to any Slack ID in the list (heuristic)."""
    # We can't check without calling Slack API, so for now always return False
    # This gets properly resolved in Step 5 (Slack sync)
    return False


# --------------------------------------------------------------------------
# Request an agent
# --------------------------------------------------------------------------


async def derive_agent_identity(
    db: AsyncSession, full_name: str
) -> tuple[str, str]:
    """Return ``(agent_id, bot_name)`` for a PI's display name.

    Both values are derived here, together, because they must agree: the
    collision prefix used to be applied to agent_id at one line and bot_name
    rebuilt from the bare last name four lines later, so Peng Wu got
    ``pwu`` / ``WuBot`` — colliding with Chunlei Wu's bot while the ids differed.
    CLAUDE.md documents ``pwu`` / ``PWuBot``.
    """
    last_name = full_name.split()[-1]
    stem = "".join(c for c in last_name.lower() if c.isalpha())
    display = last_name

    collision = await db.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == stem)
    )
    if collision.scalar_one_or_none():
        initial = full_name[0]
        return f"{initial.lower()}{stem}", f"{initial.upper()}{display}Bot"
    return stem, f"{display}Bot"


@router.post("/request")
async def request_agent(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Submit an agent request."""
    if not current_user.onboarding_complete or not current_user.profile:
        raise HTTPException(status_code=400, detail="Complete your profile first")

    existing = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == current_user.id)
    )
    if existing.scalar_one_or_none():
        return RedirectResponse(url="/agent", status_code=302)

    agent_id, bot_name = await derive_agent_identity(db, current_user.name)

    agent = AgentRegistry(
        agent_id=agent_id,
        user_id=current_user.id,
        bot_name=bot_name,
        pi_name=current_user.name,
        status="pending",
    )
    db.add(agent)
    await db.commit()

    return RedirectResponse(url="/agent", status_code=302)


# --------------------------------------------------------------------------
# Proposal review
# --------------------------------------------------------------------------


@router.post("/{agent_id}/proposals/{thread_decision_id}/review")
async def review_proposal(
    agent_id: str,
    thread_decision_id: uuid.UUID,
    request: Request,
    rating: int = Form(...),
    comment: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Rate a proposal (1-4).

    Allowed for both ``active`` and ``inactive`` agents — rating is passive
    (it only records a ``ProposalReview`` row, no Slack side effects), so an
    inactive agent's owner can still review proposals generated before the
    agent was parked.
    """
    if rating < 1 or rating > 4:
        raise HTTPException(status_code=400, detail="Rating must be 1-4")

    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)

    if agent.status not in ("active", "inactive"):
        raise HTTPException(status_code=403, detail="Agent is not active")

    td_result = await db.execute(
        select(ThreadDecision).where(ThreadDecision.id == thread_decision_id)
    )
    td = td_result.scalar_one_or_none()
    if not td:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if agent.agent_id not in (td.agent_a, td.agent_b):
        raise HTTPException(status_code=403, detail="Not your proposal")

    existing = await db.execute(
        select(ProposalReview).where(
            ProposalReview.thread_decision_id == thread_decision_id,
            ProposalReview.agent_id == agent.agent_id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Already reviewed")

    review = ProposalReview(
        thread_decision_id=thread_decision_id,
        agent_id=agent.agent_id,
        user_id=agent.user_id,  # Always the PI
        delegate_user_id=current_user.id if not is_owner else None,
        reviewed_by_user_id=current_user.id,
        rating=rating,
        comment=comment.strip() or None,
        submitted_via="web",
    )
    db.add(review)

    # Record engagement and mark any outstanding email notification as responded
    from src.services.email_notifications import mark_notification_responded, record_engagement
    await record_engagement(current_user.id, db)
    await mark_notification_responded(current_user.id, thread_decision_id, "review", db)

    await db.commit()

    return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)


@router.post("/{agent_id}/proposals/{thread_decision_id}/reopen")
async def reopen_proposal(
    agent_id: str,
    thread_decision_id: uuid.UUID,
    request: Request,
    guidance: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Reopen a proposal thread with PI guidance.

    Default behavior (``enable_private_refinement=True``): when the origin
    thread lives in a public channel, migrate it to a new ``collab_private``
    channel, post the PI's guidance there, and close the origin thread with a
    neutral ⏸️ marker — **the PI's text is never echoed into the public
    thread.** See specs/pi-interaction.md §"PI Reopens a Proposal" and
    specs/privacy-and-channel-visibility.md §Migration Rule.

    Legacy behavior (``enable_private_refinement=False``): post the PI's
    guidance verbatim into the origin thread. Retained as an emergency
    rollback lever during early rollout.
    """
    from src.config import get_settings

    guidance = guidance.strip()
    if not guidance:
        raise HTTPException(status_code=400, detail="Guidance text is required")

    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)

    # Reopening re-injects the agent into a live discussion (posts guidance to
    # Slack / spins up a private refinement channel), so it is blocked while the
    # agent is inactive — exactly the interaction that inactivating an agent is
    # meant to stop. Reactivate the agent to reopen proposals for further
    # discussion. (Unlike `review`, this requires status == 'active'.)
    #
    # Note: the reopen flow creates a collab_private channel, and the cohort gate
    # deliberately exempts those — a PI explicitly pairing two agents outranks an
    # admin-level cohort grouping. See .notes/cohort-system-v2.md §7.
    if agent.status != "active":
        raise HTTPException(
            status_code=403,
            detail="This agent is inactive. Reactivate it to reopen proposals "
            "for further discussion.",
        )

    td_result = await db.execute(
        select(ThreadDecision).where(ThreadDecision.id == thread_decision_id)
    )
    td = td_result.scalar_one_or_none()
    if not td:
        raise HTTPException(status_code=404, detail="Proposal not found")
    if agent.agent_id not in (td.agent_a, td.agent_b):
        raise HTTPException(status_code=403, detail="Not your proposal")

    # Idempotency guard. A proposal is reopened at most once per agent: the
    # dashboard hides the reopen form once a review/reopen exists, but a stale
    # page or the browser Back button can replay this POST. Without a guard the
    # replay would migrate the thread a second time and mint a duplicate
    # priv-…-N channel (or, in legacy mode, re-post the guidance to the public
    # thread). A reopen writes a rating=0 ProposalReview in the same commit as
    # refined_in_channel, so the presence of *any* review by this agent means
    # the proposal was already acted on — treat the resubmission as a no-op and
    # redirect without touching Slack.
    already_reviewed = (await db.execute(
        select(ProposalReview).where(
            ProposalReview.thread_decision_id == thread_decision_id,
            ProposalReview.agent_id == agent.agent_id,
        )
    )).scalar_one_or_none()
    if already_reviewed is not None:
        logger.info(
            "Ignoring duplicate reopen of proposal %s by %s "
            "(existing review id=%s, refined_in_channel=%s)",
            td.thread_id, agent.agent_id, already_reviewed.id, td.refined_in_channel,
        )
        return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)

    settings = get_settings()

    if settings.enable_private_refinement and td.origin_visibility == "public":
        # New behavior: migrate to a collab_private channel before any PI
        # text touches Slack.
        from src.services.private_channels import migrate_public_thread_to_private
        try:
            result = await migrate_public_thread_to_private(
                db,
                thread_decision=td,
                creator_agent_id=agent.agent_id,
                creator_pi_user=current_user,
                guidance_text=guidance,
            )
            logger.info(
                "PI %s reopened proposal %s: migrated #%s → private #%s",
                current_user.name, td.thread_id, td.channel, result.channel_name,
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Migration to private channel failed: %s", exc, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"Failed to open private refinement channel: {str(exc)[:120]}",
            )
    elif td.origin_visibility != "public":
        # Origin already private — post guidance there. (Not exercised in v1
        # since no rows have origin_visibility='collab_private' yet, but the
        # branch is defined so future migrations don't require a rewrite.)
        logger.info(
            "Proposal %s origin is already private — posting guidance in-channel",
            td.thread_id,
        )
        raise HTTPException(
            status_code=501,
            detail="Refinement on an already-private thread is not yet implemented",
        )
    else:
        # Legacy fallback: flag is off → post guidance verbatim to the origin
        # public thread. This reproduces the pre-refactor behavior and is the
        # same code as before; kept gated so rollback is a config change.
        from src.services.slack_tokens import slack_globally_enabled, token_for_agent_row

        if not await slack_globally_enabled(db):
            # Slack off → write the guidance to the DB inbox on the origin thread.
            from src.services.pi_inbox import get_latest_run_id, record_pi_message
            run_id = await get_latest_run_id(db)
            if run_id:
                await record_pi_message(
                    db, run_id=run_id, channel_name=td.channel,
                    content=f"PI guidance from {current_user.name}: {guidance}",
                    sender_name=f"{current_user.name} (PI)", thread_ts=td.thread_id,
                )
            logger.info("Reopen guidance for %s written to DB inbox (Slack off)", td.thread_id)
        else:
            try:
                # The channel lookup goes through the boundary. It used to read a
                # single 200-item page of the paginated conversations.list, so a
                # workspace with more channels than that reported "Channel not
                # found" for a channel that exists; list_channel_ids follows every
                # cursor and raises rather than returning a subset. Archived
                # channels are counted deliberately — this asks "which id owns
                # this name", not "can the bot join it".
                #
                # The post goes through it too, threaded: post_message takes
                # thread_ts precisely so this caller does not need a raw client.
                # It also splits at 4000 characters, which the raw call did not —
                # long PI guidance was silently chunked by Slack.
                from src.services.slack_web import list_channel_ids_async, post_message_async

                bot_token = token_for_agent_row(agent)
                if not bot_token:
                    raise HTTPException(status_code=500, detail="No bot token available")
                channel_id = (await list_channel_ids_async(bot_token)).get(td.channel)
                if not channel_id:
                    raise HTTPException(status_code=500, detail=f"Channel #{td.channel} not found")
                await post_message_async(
                    bot_token,
                    channel_id,
                    f"*PI guidance from {current_user.name}:*\n\n{guidance}",
                    thread_ts=td.thread_id,
                )
                logger.warning(
                    "LEGACY PATH: PI %s posted guidance in proposal thread %s via %s "
                    "(enable_private_refinement=False)",
                    current_user.name, td.thread_id, agent.agent_id,
                )
            except HTTPException:
                raise
            except Exception as exc:
                logger.error("Failed to post PI guidance to Slack: %s", exc)
                raise HTTPException(
                    status_code=500, detail=f"Failed to post to Slack: {str(exc)[:100]}",
                )

    existing = await db.execute(
        select(ProposalReview).where(
            ProposalReview.thread_decision_id == thread_decision_id,
            ProposalReview.agent_id == agent.agent_id,
        )
    )
    if not existing.scalar_one_or_none():
        review = ProposalReview(
            thread_decision_id=thread_decision_id,
            agent_id=agent.agent_id,
            user_id=agent.user_id,  # Always the PI
            delegate_user_id=current_user.id if not is_owner else None,
            reviewed_by_user_id=current_user.id,
            rating=0,  # 0 = reopened with guidance, not a rating
            comment=f"[Reopened] {guidance[:500]}",
            submitted_via="web",
        )
        db.add(review)

    # Record engagement and mark any outstanding email notification as responded
    from src.services.email_notifications import mark_notification_responded, record_engagement
    await record_engagement(current_user.id, db)
    await mark_notification_responded(current_user.id, thread_decision_id, "instruction", db)

    await db.commit()

    return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)


# --------------------------------------------------------------------------
# Private profile view/edit
# --------------------------------------------------------------------------


@router.get("/{agent_id}/conversations", response_class=HTMLResponse)
async def agent_conversations(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Read view of the agent's recent conversations + a form to post a message.

    This is the Slack-independent way for a PI to see what their agent is
    discussing and to inject a message/tag — it writes to the DB inbox, which
    the running simulation ingests. See specs/local-db-conversations.md.
    """
    from src.services.pi_inbox import get_latest_run_id

    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    if agent.status not in ("active", "inactive"):
        return RedirectResponse(url="/agent", status_code=302)
    aid = agent.agent_id

    run_id = await get_latest_run_id(db)
    channels: list[str] = []
    messages: list[dict] = []
    dms: list[dict] = []
    if run_id:
        from src.models import PiDmMessage
        dm_rows = await db.execute(
            select(PiDmMessage)
            .where(
                PiDmMessage.simulation_run_id == run_id,
                PiDmMessage.agent_id == aid,
            )
            .order_by(PiDmMessage.posted_at.desc())
            .limit(20)
        )
        dms = [
            {"direction": d.direction, "sender": d.sender_name or "", "content": d.content}
            for d in reversed(dm_rows.scalars().all())
        ]
        # Channels this agent participates in (has authored a message in).
        ch_rows = await db.execute(
            select(distinct(AgentMessage.channel_name)).where(
                AgentMessage.simulation_run_id == run_id,
                AgentMessage.agent_id == aid,
            )
        )
        channels = sorted({r[0] for r in ch_rows} | {"general"})
        # Recent messages in those channels (content is now stored in the DB).
        msg_rows = await db.execute(
            select(AgentMessage)
            .where(
                AgentMessage.simulation_run_id == run_id,
                AgentMessage.channel_name.in_(channels),
            )
            .order_by(AgentMessage.posted_at.desc())
            .limit(100)
        )
        messages = [
            {
                "channel": m.channel_name,
                "sender": m.sender_name or (m.agent_id or "PI"),
                "is_bot": m.is_bot,
                "content": m.content,
                "thread_ts": m.thread_ts,
                "posted_at": m.posted_at,
            }
            for m in reversed(msg_rows.scalars().all())
        ]
    else:
        channels = ["general"]

    return templates.TemplateResponse(
        request,
        "agent/conversations.html",
        _template_context(
            request, current_user, agent=agent, is_owner=is_owner,
            channels=channels, messages=messages, dms=dms,
            has_run=run_id is not None,
            posted=request.query_params.get("posted"),
        ),
    )


@router.post("/{agent_id}/message")
async def post_agent_message(
    agent_id: str,
    request: Request,
    channel_name: str = Form(...),
    content: str = Form(...),
    thread_ts: str = Form(""),
    tag_bot: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Write a PI-authored message into the DB inbox for the agent's workspace.

    Ingested by the running simulation via _poll_inbound_from_db — the
    Slack-independent equivalent of a PI posting in a Slack channel.
    """
    from src.services.pi_inbox import (
        get_latest_run_id,
        pi_may_post_to_channel,
        record_pi_message,
    )

    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    if agent.status != "active":
        raise HTTPException(status_code=403, detail="Agent is not active")

    text = content.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    # Optionally address the PI's own bot so it engages (same @BotName convention
    # the Slack path uses; the engine's tag detection is identical).
    if tag_bot and f"@{agent.bot_name.lower()}" not in text.lower():
        text = f"@{agent.bot_name} {text}"

    run_id = await get_latest_run_id(db)
    if not run_id:
        raise HTTPException(status_code=409, detail="No simulation run to post into yet")

    # `channel_name` is form input, so it can name any channel in the run —
    # including another pair's collab_private refinement channel. The DB-only
    # path has no Slack ACL to fall back on, so authorization is checked here
    # against private_channel_members. See specs/privacy-and-channel-visibility.md.
    target_channel = channel_name.strip() or "general"
    if not await pi_may_post_to_channel(
        db,
        run_id=run_id,
        channel_name=target_channel,
        user_id=current_user.id,
        agent_id=agent.agent_id,
    ):
        raise HTTPException(status_code=403, detail="Not a member of that channel")

    async def _write() -> None:
        await record_pi_message(
            db,
            run_id=run_id,
            channel_name=target_channel,
            content=text,
            sender_name=f"{current_user.name} (PI)",
            thread_ts=thread_ts.strip() or None,
        )
        await db.commit()

    # M1b guard: the canonical id can collide with another process (the sim)
    # minting the same microsecond for this run, which hits the
    # uq_agent_messages_run_ts constraint and would otherwise surface as a raw
    # 500. Roll back and retry once — record_pi_message mints a fresh, monotonic
    # id, so the retry gets a new ts. See PR #19 review M1.
    try:
        await _write()
    except IntegrityError:
        await db.rollback()
        try:
            await _write()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Message could not be saved due to a conflict, please retry",
            )
    logger.info("[%s] PI %s posted a web message to #%s", agent_id, current_user.name, channel_name)
    return RedirectResponse(url=f"/agent/{agent_id}/conversations?posted=1", status_code=302)


@router.post("/{agent_id}/dm")
async def send_agent_dm(
    agent_id: str,
    request: Request,
    content: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Send a DM directive to the agent's bot (standing instruction / question).

    Writes an inbound pi_dm_messages row; the sim processes it via
    _poll_pi_dms_from_db (same path as a Slack DM). See specs/local-db-conversations.md.
    """
    from src.services.pi_inbox import get_latest_run_id, record_pi_dm, web_pi_user_id

    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    if agent.status != "active":
        raise HTTPException(status_code=403, detail="Agent is not active")
    text = content.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    run_id = await get_latest_run_id(db)
    if not run_id:
        raise HTTPException(status_code=409, detail="No simulation run yet")
    await record_pi_dm(
        db, run_id=run_id, agent_id=agent_id,
        pi_user_id=web_pi_user_id(current_user.id), direction="inbound",
        content=text, sender_name=f"{current_user.name} (PI)",
    )
    await db.commit()
    logger.info("[%s] PI %s sent a web DM directive", agent_id, current_user.name)
    return RedirectResponse(url=f"/agent/{agent_id}/conversations?posted=1", status_code=302)


@router.get("/{agent_id}/profile", response_class=HTMLResponse)
async def view_private_profile(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """View agent's private profile."""
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    if agent.status != "active":
        return RedirectResponse(url="/agent", status_code=302)

    profile_path = PROFILES_DIR / "private" / f"{agent.agent_id}.md"
    content = profile_path.read_text() if profile_path.exists() else ""

    return templates.TemplateResponse(
        request,
        "agent/profile.html",
        _template_context(
            request, current_user, agent=agent, is_owner=is_owner,
            profile_content=content, editing=False,
        ),
    )


@router.get("/{agent_id}/profile/edit", response_class=HTMLResponse)
async def edit_private_profile(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Edit agent's private profile."""
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    if agent.status != "active":
        return RedirectResponse(url="/agent", status_code=302)

    profile_path = PROFILES_DIR / "private" / f"{agent.agent_id}.md"
    content = profile_path.read_text() if profile_path.exists() else ""

    return templates.TemplateResponse(
        request,
        "agent/profile.html",
        _template_context(
            request, current_user, agent=agent, is_owner=is_owner,
            profile_content=content, editing=True,
        ),
    )


@router.post("/{agent_id}/profile/save")
async def save_private_profile(
    agent_id: str,
    request: Request,
    content: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save private profile to disk and database."""
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    if agent.status != "active":
        return RedirectResponse(url="/agent", status_code=302)

    profile_path = PROFILES_DIR / "private" / f"{agent.agent_id}.md"
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(content)

    # Persist to DB — use the PI's user_id, not the delegate's
    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == agent.user_id)
    )
    profile = profile_result.scalar_one_or_none()
    if profile:
        profile.private_profile_md = content.strip() or None
        await db.commit()

    # Record revision
    from src.services.profile_versioning import create_revision
    await create_revision(
        db,
        agent_registry_id=agent.id,
        profile_type="private",
        content=content,
        changed_by_user_id=current_user.id,
        mechanism="web",
    )
    await db.commit()

    return RedirectResponse(url=f"/agent/{agent_id}/profile", status_code=302)


# --------------------------------------------------------------------------
# Public profile view/edit (PI and delegates)
# --------------------------------------------------------------------------


def _parse_list(val: str) -> list[str]:
    return [s.strip() for s in val.split(",") if s.strip()]


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
    techniques: str = Form(""),
    experimental_models: str = Form(""),
    disease_areas: str = Form(""),
    key_targets: str = Form(""),
    keywords: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save public profile changes (PI or delegate)."""
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    if agent.status != "active":
        return RedirectResponse(url="/agent", status_code=302)

    # Update the PI's profile
    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == agent.user_id)
    )
    profile = profile_result.scalar_one_or_none()
    if not profile:
        profile = ResearcherProfile(user_id=agent.user_id)
        db.add(profile)

    profile.research_summary = research_summary
    profile.techniques = _parse_list(techniques)
    profile.experimental_models = _parse_list(experimental_models)
    profile.disease_areas = _parse_list(disease_areas)
    profile.key_targets = _parse_list(key_targets)
    profile.keywords = _parse_list(keywords)
    profile.profile_version = (profile.profile_version or 0) + 1

    await db.commit()

    # Export to markdown for agent consumption (include publications)
    pi_result = await db.execute(select(User).where(User.id == agent.user_id))
    pi_user = pi_result.scalar_one()
    from src.models import Publication
    pub_result = await db.execute(
        select(Publication).where(Publication.user_id == agent.user_id)
    )
    user_pubs = list(pub_result.scalars().all())
    exported_path = export_profile_to_markdown(
        pi_user, profile, agent.agent_id, publications=user_pubs
    )

    # Record revision
    from src.services.profile_versioning import create_revision
    content = exported_path.read_text(encoding="utf-8") if exported_path else ""
    await create_revision(
        db,
        agent_registry_id=agent.id,
        profile_type="public",
        content=content,
        changed_by_user_id=current_user.id,
        mechanism="web",
    )
    await db.commit()

    logger.info(
        "Public profile for agent %s updated by %s",
        agent.agent_id, current_user.name,
    )

    return RedirectResponse(
        url=f"/agent/{agent_id}/public-profile?saved=1", status_code=302
    )


# --------------------------------------------------------------------------
# Slack connection (PI only)
# --------------------------------------------------------------------------


@router.post("/{agent_id}/slack")
async def connect_slack(
    agent_id: str,
    request: Request,
    email: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Look up the PI's Slack user ID from their email address."""
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    if not is_owner:
        raise HTTPException(status_code=403, detail="Only the PI can connect Slack")

    email = email.strip()
    slack_user_id = None
    error = None

    try:
        from src.services.slack_tokens import get_any_bot_token
        from src.services.slack_web import lookup_user_by_email_async

        bot_token = await get_any_bot_token(db)
        if not bot_token:
            error = "No Slack bot token available to perform lookup."
        else:
            # The boundary translates Slack's users_not_found into None, so "no
            # such user" is a return value here rather than a substring match on
            # an exception message.
            slack_user_id = await lookup_user_by_email_async(bot_token, email)
            if not slack_user_id:
                error = (
                    f"No Slack user found with email {email}. "
                    "Have you joined the workspace first?"
                )
    except Exception as exc:
        logger.warning("Slack lookup failed for %s: %s", email, exc)
        error = f"Slack lookup failed: {str(exc)[:100]}"

    if slack_user_id:
        agent.slack_user_id = slack_user_id
        await db.commit()
        return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)

    return RedirectResponse(
        url=f"/agent/{agent_id}/dashboard?slack_error=" + (error or "Unknown error"),
        status_code=302,
    )


def _resolve_delegate_names(slack_ids: list[str], bot_token: str | None) -> list[dict]:
    """Resolve Slack user IDs to display names using the given bot token.

    A name that will not resolve falls back to the raw id — this only feeds the
    dashboard's delegate list, so one unresolvable id must not blank the rest.
    """
    from src.services.slack_web import get_user_info

    if not bot_token:
        return [{"slack_id": sid, "name": sid} for sid in slack_ids]

    delegates = []
    for sid in slack_ids:
        info = None
        try:
            # Returns None for a user Slack does not know, so the fallback below
            # covers both "no such user" and a failed call.
            info = get_user_info(bot_token, sid)
        except Exception as exc:
            logger.warning("Could not resolve Slack display name for %s: %s", sid, exc)
        info = info or {}
        delegates.append({
            "slack_id": sid,
            "name": info.get("real_name") or info.get("name") or sid,
        })
    return delegates


# --------------------------------------------------------------------------
# Delegate Slack connection
# --------------------------------------------------------------------------


@router.post("/{agent_id}/delegates/connect-slack")
async def delegate_connect_slack(
    agent_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Let a delegate link their Slack account to this agent."""
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)

    if not current_user.email:
        return RedirectResponse(
            url=f"/agent/{agent_id}/dashboard?slack_error=No email on your account.",
            status_code=302,
        )

    error = None
    try:
        from src.services.slack_tokens import get_any_bot_token
        from src.services.slack_web import lookup_user_by_email_async

        bot_token = await get_any_bot_token(db)
        if not bot_token:
            error = "No Slack bot token available."
        else:
            # None means Slack has no such user (the boundary translates
            # users_not_found), so the "join the workspace first" message is
            # driven by a value rather than by a substring of an exception.
            sid = await lookup_user_by_email_async(bot_token, current_user.email)
            if not sid:
                error = (
                    f"No Slack account found for {current_user.email}. "
                    "Please join the workspace first."
                )
            else:
                current_ids = list(agent.delegate_slack_ids or [])
                if sid not in current_ids:
                    current_ids.append(sid)
                    agent.delegate_slack_ids = current_ids
                    await db.commit()
                return RedirectResponse(
                    url=f"/agent/{agent_id}/dashboard", status_code=302
                )
    except Exception as exc:
        logger.warning(
            "Delegate Slack lookup failed for %s: %s", current_user.email, exc
        )
        error = f"Slack lookup failed: {str(exc)[:100]}"

    return RedirectResponse(
        url=f"/agent/{agent_id}/dashboard?slack_error=" + (error or "Unknown error"),
        status_code=302,
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
    """Send delegate invitation(s) by email."""
    import re
    import secrets
    from datetime import datetime, timedelta

    from src.config import get_settings
    from src.models import DelegateInvitation
    from src.services.email import send_delegate_invitation

    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
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

    errors = []
    sent_count = 0
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

        # Send email (non-blocking — invitation is created regardless)
        invite_url = f"{settings.base_url}/invite/{token}"
        send_delegate_invitation(email, agent.pi_name, agent.bot_name, invite_url)
        sent_count += 1

    await db.commit()

    error_msg = "; ".join(errors) if errors else ""
    if error_msg:
        return RedirectResponse(
            url=f"/agent/{agent_id}/dashboard?delegate_error={error_msg}",
            status_code=302,
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
    from src.models import DelegateInvitation

    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
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
        # Remove Slack ID if present
        if delegate.user.email and agent.delegate_slack_ids:
            try:
                from src.services.slack_tokens import get_any_bot_token
                from src.services.slack_web import lookup_user_by_email_async

                bot_token = await get_any_bot_token(db)
                if bot_token:
                    sid = await lookup_user_by_email_async(bot_token, delegate.user.email)
                    current_ids = list(agent.delegate_slack_ids or [])
                    if sid and sid in current_ids:
                        current_ids.remove(sid)
                        agent.delegate_slack_ids = current_ids if current_ids else None
            except Exception as exc:
                logger.warning("Delegate Slack sync is best-effort; skipped: %s", exc)

        await db.delete(delegate)
        await db.commit()
        logger.info(
            "Delegate %s removed from agent %s by %s",
            delegate.user_id, agent.agent_id, current_user.name,
        )

    return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)
