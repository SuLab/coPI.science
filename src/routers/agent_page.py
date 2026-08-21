"""My Agent page router."""

import asyncio
import logging
import re
import uuid
from datetime import UTC

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import distinct, func, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.database import get_db
from src.dependencies import get_agent_with_access, get_current_user, get_pi_user
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

SLACK_INVITE_URL = (
    "https://join.slack.com/t/labbot-workspace/shared_invite/"
    "zt-3sxfrrisw-t4hRz4aMfZZPxThxUaTGKA"
)

# Thread roots per page. The window's unit is threads, not messages: replies no
# longer consume slots, so this surfaces more distinct conversations than the
# previous flat 100-message window did.
_ROOT_LIMIT = 50


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

    try:
        # Record engagement and mark any outstanding email notification as
        # responded. These run inside the same try as the commit — and MUST,
        # because their SELECTs trigger SQLAlchemy's autoflush of the pending
        # ProposalReview insert above, so on a lost race the IntegrityError
        # can surface here rather than at the explicit commit() below.
        from src.services.email_notifications import mark_notification_responded, record_engagement
        await record_engagement(current_user.id, db)
        await mark_notification_responded(current_user.id, thread_decision_id, "review", db)

        await db.commit()
    except IntegrityError:
        # Lost the race on uq_proposal_reviews_decision_agent (double-click,
        # two tabs): a review for this decision+agent now exists. The
        # rollback also discards THIS request's record_engagement /
        # mark_notification_responded writes — correct, because the winning
        # racer performed its own. Same outcome as the SELECT guard above.
        await db.rollback()
        return RedirectResponse(
            url=f"/agent/{agent_id}/dashboard", status_code=302
        )

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
    """Record the PI's guidance on a proposal and mark it reopened.

    The guidance is written into the proposal's origin thread's DB inbox —
    visible on the read-only ``/conversations`` page — and a rating=0
    ``ProposalReview`` is filed so the dashboard stops treating the proposal as
    unreviewed. Nothing re-engages the bot: the 2026-08-12 PI-interaction
    removal cycle deleted both the Slack post this route used to make (a bot
    token no longer changes what happens here) and the engine-side consumers
    that would have treated the posted text as authoritative
    (``has_pi_directive``/``pi_priority``/``pi_context`` are gone from
    ``src/agent/state.py``; the guidance can never set a bot's pending state or
    reactive priority (``MessageLog.has_new_reply_from_other`` filters human
    rows unconditionally), and it can never activate a new thread either
    (``SimulationEngine._phase3_activate_threads`` filters human rows before
    acting on them) — see ``src/agent/message_log.py`` /
    ``src/agent/simulation.py``). This route
    never creates a NEW collab_private channel either: the engine-side
    private-channel collaboration/refinement flow
    (``src/services/private_channels.py``) was deleted in the same audit wave
    (fix 9 — "private-channel collaboration is out"; see
    docs/plans/2026-08-12-pr34-pitch-only-reconciliation-design.md §8/§15).
    See specs/pi-interaction.md §"PI Reopens a Proposal" and
    specs/privacy-and-channel-visibility.md §Migration Rule for the
    now-inapplicable design intent those specs still describe.
    """
    guidance = guidance.strip()
    if not guidance:
        raise HTTPException(status_code=400, detail="Guidance text is required")

    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)

    # Blocked while the agent is inactive, matching every other write path that
    # touches a live agent's workspace. Reactivate the agent to reopen
    # proposals for further discussion. (Unlike `review`, this requires
    # status == 'active'.)
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
    # replay would re-post the guidance into the origin thread a second time. A
    # reopen writes a rating=0 ProposalReview in the same commit as its post, so
    # the presence of *any* review by this agent means the proposal was already
    # acted on — treat the resubmission as a no-op and redirect without
    # writing a second inbox row.
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

    # Post the guidance directly into the origin thread's DB inbox. This is
    # the only path now: no Slack post (removed 2026-08-12 — the engine has no
    # PI-bot interaction surface left for it to reach), and no collab_private
    # migration branch, regardless of td.origin_visibility -- see the
    # docstring above for why.
    from src.services.pi_inbox import get_latest_run_id, record_pi_message
    run_id = await get_latest_run_id(db)
    if run_id:
        await record_pi_message(
            db, run_id=run_id, channel_name=td.channel,
            content=f"PI guidance from {current_user.name}: {guidance}",
            sender_name=f"{current_user.name} (PI)", thread_ts=td.thread_id,
        )
    logger.info("Reopen guidance for %s written to DB inbox", td.thread_id)

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
    from src.services.conversation_feed import own_or_gated, resolve_agent_gate
    from src.services.pi_inbox import get_latest_run_id

    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    if agent.status not in ("active", "inactive"):
        return RedirectResponse(url="/agent", status_code=302)
    aid = agent.agent_id

    run_id = await get_latest_run_id(db)
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
    UNGATED (``src/agent/message_log.py:224-226``) because it is thread-internal;
    here the whole point is that out-of-cohort traffic must not become reachable
    by clicking, and a future reader should not "fix" this back toward engine
    parity.
    """
    from src.services.conversation_feed import own_or_gated, resolve_agent_gate
    from src.services.pi_inbox import get_latest_run_id

    agent, _is_owner = await get_agent_with_access(agent_id, db, current_user)
    if agent.status not in ("active", "inactive"):
        raise HTTPException(status_code=404)
    aid = agent.agent_id

    run_id = await get_latest_run_id(db)
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
        # Flush the row into existence before the SQL-side bump below: on a
        # pending object the expression would render inside the INSERT's VALUES,
        # which cannot reference its own target table ("invalid reference to
        # FROM-clause entry for table researcher_profiles").
        await db.flush()

    profile.research_summary = research_summary
    profile.techniques = _parse_list(techniques)
    profile.experimental_models = _parse_list(experimental_models)
    profile.disease_areas = _parse_list(disease_areas)
    profile.key_targets = _parse_list(key_targets)
    profile.keywords = _parse_list(keywords)
    # SQL-side increment: the Python read-modify-write lost updates when two
    # writers raced (issue #22 C1). Nothing below reads profile_version, so the
    # expiry the expression assignment causes needs no refresh here.
    profile.profile_version = func.coalesce(ResearcherProfile.profile_version, 0) + 1

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
                # Atomic, self-deduplicating append: the read-append-reassign
                # this replaces wrote the WHOLE array back, so two delegates
                # linking at once each dropped the other's id (issue #22 C1).
                # The dedup guard has to live in the SQL — a check-then-append
                # in Python just re-races. Commit unconditionally now: when the
                # id is already present the UPDATE matches no row and the
                # commit is a no-op.
                from sqlalchemy import text as sa_text
                from sqlalchemy import update as sa_update
                await db.execute(
                    sa_update(AgentRegistry)
                    .where(
                        AgentRegistry.id == agent.id,
                        sa_text(
                            "NOT (coalesce(delegate_slack_ids, '{}'::varchar[]) @> ARRAY[:sid]::varchar[])"
                        ).bindparams(sid=sid),
                    )
                    .values(
                        delegate_slack_ids=sa_text(
                            "array_append(coalesce(delegate_slack_ids, '{}'::varchar[]), :sid2)"
                        ).bindparams(sid2=sid)
                    )
                )
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
                    # Atomic removal, for the same reason as the append above:
                    # the read-remove-reassign wrote the whole array back and
                    # lost a concurrent append (issue #22 C1). NULLIF preserves
                    # the old "empty means NULL" shape of this column.
                    from sqlalchemy import text as sa_text
                    from sqlalchemy import update as sa_update
                    if sid:
                        await db.execute(
                            sa_update(AgentRegistry)
                            .where(AgentRegistry.id == agent.id)
                            .values(
                                delegate_slack_ids=sa_text(
                                    "nullif(array_remove(coalesce(delegate_slack_ids, '{}'::varchar[]), :sid), '{}'::varchar[])"
                                ).bindparams(sid=sid)
                            )
                        )
            except Exception as exc:
                logger.warning("Delegate Slack sync is best-effort; skipped: %s", exc)

        await db.delete(delegate)
        await db.commit()
        logger.info(
            "Delegate %s removed from agent %s by %s",
            delegate.user_id, agent.agent_id, current_user.name,
        )

    return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)
