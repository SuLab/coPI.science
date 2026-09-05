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
from sqlalchemy import distinct, func, select, tuple_
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
from src.services.atomic_write import atomic_write_text
from src.services.profile_export import export_profile_to_markdown
from src.services.profile_pipeline import bump_profile_version
from src.services.validators import is_valid_email

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")

PROFILES_DIR = Path("profiles")
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
            ProposalReview.agent_id == aid,
            ProposalReview.rating != -1,
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

    A THIRD same-initial namesake collides on the prefixed candidate too
    (issue #26 C1): fall back to a numeric suffix appended to the prefixed
    candidate (``pwu2``, ``pwu3``, ...), matching
    ``scripts/backfill_agents.py``'s ``_resolve_agent_id``/``_bot_name_for``.
    """
    last_name = full_name.split()[-1]
    stem = "".join(c for c in last_name.lower() if c.isalpha())
    display = last_name

    collision = await db.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == stem)
    )
    if not collision.scalar_one_or_none():
        return stem, f"{display}Bot"

    initial = full_name[0]
    prefixed = f"{initial.lower()}{stem}"
    collision = await db.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == prefixed)
    )
    if not collision.scalar_one_or_none():
        return prefixed, f"{initial.upper()}{display}Bot"

    for i in range(2, 20):
        candidate = f"{prefixed}{i}"
        collision = await db.execute(
            select(AgentRegistry).where(AgentRegistry.agent_id == candidate)
        )
        if not collision.scalar_one_or_none():
            return candidate, f"{initial.upper()}{display}{i}Bot"
    raise HTTPException(
        status_code=409,
        detail="Could not derive a unique agent identity, please contact support",
    )


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
    try:
        await db.commit()
    except IntegrityError as exc:
        # Lost a race on the agent_id unique constraint — another request
        # committed the same derived identity between our SELECT and our
        # commit. Retrying would recompute the identical candidate from the
        # same now-stale read, so fail fast instead of looping. See #26 C1.
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="An agent request for this identity is already in progress, please retry",
        ) from exc

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
    from datetime import datetime

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
    existing_row = existing.scalar_one_or_none()
    if existing_row is not None and existing_row.rating != -1:
        # A delegate (or the PI) may have already reviewed. Retire every recipient's
        # outstanding notification for this agent + proposal before rejecting --
        # otherwise whoever loses this race has no other web path to clear their
        # notification (V4-4b).
        from src.services.email_notifications import mark_notification_responded, record_engagement
        await record_engagement(current_user.id, db)
        await mark_notification_responded(agent.id, thread_decision_id, "review", db)
        await db.commit()
        raise HTTPException(status_code=400, detail="Already reviewed")

    # current_user.id / agent.id captured BEFORE the try (V4-4b, Task 21.13): the
    # loser's own db.add()/flush() failure expires every attribute of every object
    # this session is tracking, INCLUDING current_user's and agent's primary keys --
    # reproduced directly against a real Postgres fixture: bare current_user.id /
    # agent.id in the except arm below, even AFTER its own db.rollback(), raises
    # sqlalchemy.exc.MissingGreenlet on this async session (same class of bug as Task
    # 21.2/21.10's rollback fixes; there is no implicit re-fetch on an AsyncSession).
    # Names avoid colliding with the `agent_id` path parameter (the string slug, e.g.
    # "alpha") already in scope. agent_agent_id / pi_user_id (I3, #24 V5-2) are the
    # same story: the except arm's D6 upgrade needs agent.agent_id / agent.user_id
    # again after the rollback that would otherwise expire them.
    current_user_id = current_user.id
    agent_registry_id = agent.id
    agent_agent_id = agent.agent_id
    pi_user_id = agent.user_id

    # Import hoisted ABOVE the try (V4-4b, Task 21.13): the except arm below also
    # needs record_engagement/mark_notification_responded to retire the race LOSER's
    # own notification, and a local import inside the try body is not in scope in the
    # except.
    from src.services.email_notifications import (
        mark_notification_responded,
        record_engagement,
    )

    # V5: two concurrent first-time reviews for the same (thread_decision, agent)
    # both pass the SELECT guard above and then race on
    # uq_proposal_reviews_decision_agent. Autoflush (default True; see
    # src/database.py:39-43) fires on the NEXT db.execute after db.add() below --
    # which is record_engagement's SELECT, not the final commit -- so the loser's
    # IntegrityError surfaces there. The guard therefore has to span from db.add
    # through commit, not just wrap commit() the way the vote endpoint does.
    #
    # D6/COR-13: existing_row is not None here only when its rating == -1 (the != -1
    # case returned 400 above) -- the engine's implicit marker (Task 20.9), upgraded
    # in place instead of a second insert (proposal_reviews has a real UNIQUE
    # (thread_decision_id, agent_id)). id is left untouched; reviewed_at is bumped to
    # record when the explicit action happened, not when the engine wrote the
    # implicit marker. An update cannot raise IntegrityError (no new row), so the
    # guard below is simply inert on this path -- no serialization on this path,
    # two simultaneous first explicit reviews over a -1 marker both update, last
    # writer wins (accepted by the D6 ruling).
    try:
        if existing_row is not None:
            existing_row.user_id = agent.user_id  # Always the PI
            existing_row.delegate_user_id = current_user_id if not is_owner else None
            existing_row.reviewed_by_user_id = current_user_id
            existing_row.rating = rating
            existing_row.comment = comment.strip() or None
            existing_row.submitted_via = "web"
            existing_row.reviewed_at = datetime.now(UTC)
        else:
            review = ProposalReview(
                thread_decision_id=thread_decision_id,
                agent_id=agent.agent_id,
                user_id=agent.user_id,  # Always the PI
                delegate_user_id=current_user_id if not is_owner else None,
                reviewed_by_user_id=current_user_id,
                rating=rating,
                comment=comment.strip() or None,
                submitted_via="web",
            )
            db.add(review)

        # Record engagement and mark any outstanding email notification as responded
        await record_engagement(current_user_id, db)
        await mark_notification_responded(agent_registry_id, thread_decision_id, "review", db)

        await db.commit()
    except IntegrityError:
        await db.rollback()
        # I3 (#24 V5-2 / D6): the reachable race here is web-vs-engine, not
        # web-vs-web -- _persist_implicit_proposal_review (a separate process, its own
        # session) inserts rating=-1 between this request's guard SELECT (:526-532,
        # which already treats -1 as not-yet-reviewed) and this request's flush.
        # Unconditionally raising "Already reviewed" would be false under D6 (a -1 row
        # is not a decision) and would throw away the PI's rating/comment for nothing.
        # Re-select the winning row: if it is still the engine's implicit marker,
        # upgrade it in place -- the same six fields + reviewed_at the happy-path
        # insert/update above would have set -- and commit (mirrors the vote endpoint,
        # src/routers/public.py:1083-1099). Only a REAL winning review (rating != -1)
        # is actually "already reviewed".
        # N2 (#24 closure audit): scalar_one_or_none(), not scalar_one() -- an
        # IntegrityError that was NOT the review-uniqueness conflict (e.g. an FK or
        # NOT-NULL violation from a concurrently deleted ThreadDecision/User) finds no
        # winning row here, and scalar_one() would raise NoResultFound -- a 500 out of
        # the very handler that exists to avoid one. Mirrors reopen_proposal's sibling
        # recovery (:942-969: scalar_one_or_none() + an else that logs and continues
        # rather than crashing).
        winner = (await db.execute(
            select(ProposalReview).where(
                ProposalReview.thread_decision_id == thread_decision_id,
                ProposalReview.agent_id == agent_agent_id,
            )
        )).scalar_one_or_none()
        if winner is not None and winner.rating == -1:
            winner.user_id = pi_user_id  # Always the PI
            winner.delegate_user_id = current_user_id if not is_owner else None
            winner.reviewed_by_user_id = current_user_id
            winner.rating = rating
            winner.comment = comment.strip() or None
            winner.submitted_via = "web"
            winner.reviewed_at = datetime.now(UTC)
            # A real review WAS just filed by this request -- it only lost the INSERT
            # race to the engine's own marker -- so retire the notification exactly as
            # the happy path above does for every successful review, insert or upgrade.
            await record_engagement(current_user_id, db)
            await mark_notification_responded(agent_registry_id, thread_decision_id, "review", db)
            await db.commit()
            return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)

        if winner is None:
            # No winning row means this IntegrityError was not the uniqueness conflict
            # at all (N2), so there is nothing to reject as "already reviewed" -- but
            # it also means the rollback above threw THIS request's insert away and
            # nothing took its place, so the PI's rating and comment were persisted
            # nowhere. R8 / #24 V5: this arm used to log, retire the notification, and
            # return the same 302 the success path returns, byte for byte. The PI saw
            # the normal post-review page with their text gone, and because
            # mark_notification_responded had just flipped the outstanding
            # EmailNotification to 'responded', no reminder chased the proposal
            # either. Two changes, ruled in
            # docs/plans/2026-09-04-decisions/task-14.md:
            #
            # 1. A coded 409 instead of the redirect, so the PI is told. The carrier
            #    copied is post_agent_message's own rollback-then-409 (:1461-1466) --
            #    the in-repo pattern issue #24's Fix clause names ("rollback + one
            #    retry then 409"). Deliberately NOT the ?slack_error= /
            #    ?delegate_error= redirect carriers this file also owns: dashboard.html
            #    renders both only inside `{% if agent.status == 'active' %}` (:279),
            #    and delegate_error only inside `{% if is_owner %}` (:330), while this
            #    handler admits inactive agents (:514, on purpose -- see the docstring)
            #    and delegates, so a query parameter would be silently dropped for
            #    exactly the users this branch strands. `from None` matches the
            #    "Already reviewed" raise below and keeps ruff's B904 quiet.
            # 2. NO mark_notification_responded. Nothing was persisted, so the
            #    reminder has to keep chasing the proposal. Leaving the row 'sent' is
            #    the state that would have existed had the request never happened, and
            #    every consumer already handles it: _process_user_notifications sends
            #    nothing while the row is inside its reply window and then expires and
            #    re-sends by RECONCILING that same row rather than inserting a second
            #    one (email_notifications.py:274-289, :550-565), so there is neither a
            #    duplicate send nor a stuck sweep; and a later successful review still
            #    retires it, because mark_notification_responded filters on
            #    status == 'sent'.
            #
            # record_engagement stays, and is still committed: the PI did act, and all
            # that call does is reset consecutive_missed / last_engagement_at, so
            # dropping it would count our own write failure against them and eventually
            # downgrade their e-mail frequency (_check_engagement_and_downgrade).
            logger.error(
                "IntegrityError on proposal %s review write but no winning row was "
                "found on re-select -- nothing was persisted, so the reviewer gets a "
                "409 and the outstanding reminder is left alone",
                thread_decision_id,
            )
            await record_engagement(current_user_id, db)
            await db.commit()
            raise HTTPException(
                status_code=409,
                detail="Your review could not be saved due to a conflict, please retry",
            ) from None

        # A real review won the race. Their review is the decision for this agent, so
        # still retire THIS responder's outstanding notification (V4-4b) before
        # bouncing them -- the rollback above threw away the retire that ran inside
        # the try.
        await record_engagement(current_user_id, db)
        await mark_notification_responded(agent_registry_id, thread_decision_id, "review", db)
        await db.commit()
        raise HTTPException(status_code=400, detail="Already reviewed") from None

    return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)


@router.post("/{agent_id}/proposals/{thread_decision_id}/reopen")
async def reopen_proposal(
    agent_id: str,
    thread_decision_id: uuid.UUID,
    request: Request,
    # Form("") not Form(...), for the same reason as save_private_profile's `content`
    # below (phase8 M2): an empty textarea submits `guidance=`, which Starlette's form
    # parser hands to FastAPI as a MISSING field, so a required parameter answers a raw
    # 422 JSON body -- `{"detail":[{"type":"missing","loc":["body","guidance"],...}]}` --
    # and the coded 400 six lines down was unreachable from the browser. Measured: both
    # an empty box and an omitted field 422'd. With the default the handler sees "",
    # strips it, and returns its own "Guidance text is required". (Unlike that route,
    # nothing here needs to tell "submitted empty" from "field omitted": both are
    # rejected, so no raw-FormData presence check is required.)
    guidance: str = Form(""),
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
    from datetime import datetime

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
    # refined_in_channel, so any *explicit* review (rating ≠ -1) by this agent means
    # the proposal was already acted on — treat the resubmission as a no-op and
    # redirect without touching Slack.
    already_reviewed = (await db.execute(
        select(ProposalReview).where(
            ProposalReview.thread_decision_id == thread_decision_id,
            ProposalReview.agent_id == agent.agent_id,
        )
    )).scalar_one_or_none()
    if already_reviewed is not None and already_reviewed.rating != -1:
        logger.info(
            "Ignoring duplicate reopen of proposal %s by %s "
            "(existing review id=%s, refined_in_channel=%s)",
            td.thread_id, agent.agent_id, already_reviewed.id, td.refined_in_channel,
        )
        # Same reasoning as review_proposal's "Already reviewed" branch (V4-4b):
        # retire every recipient's outstanding notification for this agent + proposal
        # before bouncing them.
        from src.services.email_notifications import mark_notification_responded, record_engagement
        await record_engagement(current_user.id, db)
        await mark_notification_responded(agent.id, thread_decision_id, "instruction", db)
        await db.commit()
        return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)

    # Second idempotency guard, on the migration itself (#24 N1-b / #21 COR-19.6).
    # The review-row check above is not sufficient, because the review row is exactly
    # what a lost race destroys: migrate_public_thread_to_private commits its own
    # AgentChannel/member/handover rows and `refined_in_channel` as soon as its side
    # effects are irreversible (private_channels.py:644, and :415 on the Slack-off
    # path), then the `except IntegrityError` arm below rolls back this route's own
    # ProposalReview insert. A retry therefore finds no review row, and
    # `origin_visibility` still 'public' -- the migration never flips it -- so without
    # this it migrated a SECOND time. Slack does not refuse the second create:
    # AgentSlackClient.create_private_channel (slack_client.py:933-936) appends a fresh
    # per-call `%Y%m%d-%H%M%S` stamp to the otherwise-deterministic
    # priv-{a}-{b}-{origin} slug, so the retry asks for a name that never existed. The
    # first channel -- which already holds the handover and the PI's guidance -- is
    # then orphaned, with both bots still in it and refined_in_channel repointed away.
    # `refined_in_channel` is the durable record that the migration happened, so read
    # it. `d1146a4` added this same guard to the e-mail twin
    # (email_inbound.py:878-886) and stopped there; the two now read the same.
    if td.refined_in_channel:
        logger.info(
            "Proposal %s was already migrated to %s on an earlier attempt -- not "
            "migrating again; the guidance is already in that channel",
            td.thread_id, td.refined_in_channel,
        )
        already_migrated = True
    else:
        already_migrated = False

    settings = get_settings()

    # Set only by the legacy Slack-off branch below, and only there because that is the
    # one branch whose PI text lives in a row this route itself must commit. Declared
    # here so the `except IntegrityError` arm can read it whichever branch ran.
    inbox_row: tuple[uuid.UUID, str, str, str, str] | None = None

    if already_migrated:
        # Nothing to do on Slack: the first attempt's migration posted this same
        # guidance into the private channel as part of its handover. Fall through so
        # the route still records the review row and retires the notification (again,
        # matching the e-mail twin) -- skipping the whole request instead would leave
        # the proposal looking unreviewed for ever.
        pass
    elif settings.enable_private_refinement and td.origin_visibility == "public":
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
                # Held for the recovery arm below (#24 V5 iii). record_pi_message does
                # not commit -- deliberately, because the e-mail twin needs this row to
                # ride the same commit that retires the notification, so committing it
                # early would let a retried S3 delivery write a second one (the #21
                # COR-19.6 shape). It is the ONLY copy of what the PI typed on this
                # path: with Slack off the DB inbox is the whole conversation store and
                # nothing was posted anywhere. A rollback() below therefore destroys the
                # guidance outright unless the arm re-creates it, which is why the
                # arguments are captured rather than rebuilt (`td`/`current_user` are
                # expired by that rollback, and re-deriving the text would duplicate the
                # two strings the engine reads).
                inbox_content = f"PI guidance from {current_user.name}: {guidance}"
                inbox_sender = f"{current_user.name} (PI)"
                await record_pi_message(
                    db, run_id=run_id, channel_name=td.channel,
                    content=inbox_content, sender_name=inbox_sender,
                    thread_ts=td.thread_id,
                )
                inbox_row = (
                    run_id, td.channel, inbox_content, inbox_sender, td.thread_id,
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
    existing_row = existing.scalar_one_or_none()

    # Captured BEFORE the try for the same reason as review_proposal's V4-4b guard
    # (:544-554 above): a lost race's IntegrityError expires every attribute of every
    # object this session is tracking, including agent's and current_user's primary
    # keys, and this async session has no implicit re-fetch on attribute access after
    # rollback() -- bare agent.user_id / current_user.id in the except arm below would
    # raise sqlalchemy.exc.MissingGreenlet.
    current_user_id = current_user.id
    agent_registry_id = agent.id
    agent_agent_id = agent.agent_id
    pi_user_id = agent.user_id
    # The human thread id, for the recovery arm's log lines. Read here for the same
    # reason as the four above: `td` is expired by the rollback. The arm's own
    # re-select cannot supply it -- the row may be gone by then, which is precisely the
    # case that used to 500 (#24 V5 i).
    td_thread_id = td.thread_id

    # Import hoisted ABOVE the try (mirrors review_proposal, :556-563): the except arm
    # below also needs record_engagement/mark_notification_responded, and a local
    # import inside the try body is not in scope in the except.
    from src.services.email_notifications import (
        mark_notification_responded,
        record_engagement,
    )

    # I2 (#24 V5): mirrors review_proposal's guard exactly. The `existing_row is None`
    # branch's db.add() can lose a race against a concurrent insert for the same
    # (thread_decision_id, agent_id) -- the engine's implicit marker, an e-mail reply,
    # a delegate's /review -- surfaced by autoflush at record_engagement, same as
    # review_proposal three hundred lines above. The two update branches (rating == -1
    # upgrade; real review left alone) touch no new row, so they cannot raise
    # IntegrityError -- the guard is inert there.
    try:
        if existing_row is None:
            review = ProposalReview(
                thread_decision_id=thread_decision_id,
                agent_id=agent_agent_id,
                user_id=pi_user_id,  # Always the PI
                delegate_user_id=current_user_id if not is_owner else None,
                reviewed_by_user_id=current_user_id,
                rating=0,  # 0 = reopened with guidance, not a rating
                comment=f"[Reopened] {guidance[:500]}",
                submitted_via="web",
            )
            db.add(review)
        elif existing_row.rating == -1:
            # D6/COR-13: this SELECT happens AFTER migrate_public_thread_to_private (a
            # multi-call Slack round-trip), so -- unlike the first SELECT above, whose
            # != -1 case returns early -- the invariant is re-checked here rather than
            # assumed: a real review can be filed by the PI, a delegate, or an e-mail
            # reply while that round-trip is in flight. Only the engine's implicit
            # marker (rating == -1) is safe to upgrade in place; id is left untouched,
            # reviewed_at is bumped to record when the explicit action happened.
            existing_row.user_id = pi_user_id  # Always the PI
            existing_row.delegate_user_id = current_user_id if not is_owner else None
            existing_row.reviewed_by_user_id = current_user_id
            existing_row.rating = 0  # 0 = reopened with guidance, not a rating
            existing_row.comment = f"[Reopened] {guidance[:500]}"
            existing_row.submitted_via = "web"
            existing_row.reviewed_at = datetime.now(UTC)
        else:
            # A real review was filed for this (thread_decision, agent) while the
            # migration ran -- leave it alone rather than overwrite it with the
            # reopen marker (D6/COR-13).
            logger.warning(
                "Proposal %s gained a review (rating=%s) while the reopen migration "
                "ran -- leaving it alone",
                td.thread_id, existing_row.rating,
            )

        # Record engagement and mark any outstanding email notification as responded
        await record_engagement(current_user_id, db)
        await mark_notification_responded(agent_registry_id, thread_decision_id, "instruction", db)

        await db.commit()
    except IntegrityError:
        await db.rollback()
        # Someone else (the engine's implicit marker, an e-mail reply, a delegate's
        # /review) won the race on uq_proposal_reviews_decision_agent. Do NOT re-run
        # the migration and do NOT re-raise.
        #
        # Does the decision itself still exist? This re-select used to exist to re-bind
        # `refined_in_channel` from a value captured before the try, because
        # migrate_public_thread_to_private only FLUSHED it and this rollback() undid
        # the flush -- orphaning a Slack channel that existed for real. Both migration
        # paths now COMMIT it themselves as soon as their side effects are irreversible
        # (private_channels.py:415 and :644), so there is nothing left to restore and
        # the re-bind is gone: it could only ever write back the value it had just
        # read. Measured, not argued -- a before_cursor_execute recorder shows the SQL
        # this arm emits after `ROLLBACK TO SAVEPOINT` contains no UPDATE of
        # thread_decisions at all (verify-deploy finding 1a), and
        # test_refined_in_channel_survives_the_lost_race_without_the_arm_re_binding_it
        # pins the durability that replaced it. What the query is kept for is the
        # diagnosis below: not every IntegrityError out of the block above is the
        # review-uniqueness conflict, and a missing decision (an FK violation from a
        # concurrent delete) is the one shape that explains an empty `winner`.
        #
        # scalar_one_or_none(), not scalar_one(): on exactly that shape scalar_one()
        # raised NoResultFound -- a 500 out of the very arm that exists to avoid one
        # (#24 V5 i). N2 fixed the identical line in review_proposal's arm (:626-638)
        # and left this one, which is the handler its own comment points at.
        decision_still_exists = (await db.execute(
            select(ThreadDecision.id).where(ThreadDecision.id == thread_decision_id)
        )).scalar_one_or_none() is not None

        winner = (await db.execute(
            select(ProposalReview).where(
                ProposalReview.thread_decision_id == thread_decision_id,
                ProposalReview.agent_id == agent_agent_id,
            )
        )).scalar_one_or_none()
        if winner is not None and winner.rating == -1:
            # Same D6/COR-13 upgrade as the try body above -- the winning row is the
            # engine's implicit marker, not a real decision.
            winner.user_id = pi_user_id
            winner.delegate_user_id = current_user_id if not is_owner else None
            winner.reviewed_by_user_id = current_user_id
            winner.rating = 0
            winner.comment = f"[Reopened] {guidance[:500]}"
            winner.submitted_via = "web"
            winner.reviewed_at = datetime.now(UTC)
        elif winner is not None:
            logger.warning(
                "Proposal %s gained a review (rating=%s) while the reopen write "
                "raced -- leaving it alone",
                td_thread_id, winner.rating,
            )
        elif decision_still_exists:
            logger.error(
                "IntegrityError on proposal %s reopen write but no winning row was "
                "found on re-select and the decision still exists -- unexpected",
                td_thread_id,
            )
        else:
            logger.warning(
                "Proposal %s was deleted while its reopen was being written; the "
                "IntegrityError was that foreign key, not a duplicate review",
                td_thread_id,
            )

        if inbox_row is not None:
            # #24 V5 (iii). On the legacy Slack-off path the rollback() above just
            # destroyed the PI's guidance: record_pi_message adds the agent_messages
            # row and leaves the commit to this route (pi_inbox.py:159-173 spells out
            # why it must, and names this caller), and with Slack off that row is the
            # only place the text exists -- nothing was posted anywhere else. Re-create
            # it on the same commit as the recovery below, so the engine's inbound
            # poller still sees the guidance it would have seen had the race not
            # happened. Deliberately NOT committed inside record_pi_message: the e-mail
            # twin (email_inbound.py) retires the notification on the same commit as
            # this row, and an early commit there would let a retried S3 delivery mint
            # a second guidance row (#21 COR-19.6).
            from src.services.pi_inbox import record_pi_message
            inbox_run, inbox_channel, inbox_text, inbox_from, inbox_thread = inbox_row
            await record_pi_message(
                db, run_id=inbox_run, channel_name=inbox_channel, content=inbox_text,
                sender_name=inbox_from, thread_ts=inbox_thread,
            )

        await record_engagement(current_user_id, db)
        await mark_notification_responded(agent_registry_id, thread_decision_id, "instruction", db)
        await db.commit()
        logger.warning(
            "Proposal %s reopen write lost a race; the migration's own rows and "
            "refined_in_channel are already committed, so the recovery is the review "
            "row (and, on the Slack-off path, the PI's inbox guidance) -- not a "
            "re-migration",
            td_thread_id,
        )

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
    from src.services.conversation_feed import own_or_gated, resolve_agent_gate
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
            # Total ordering. posted_at alone is not one: pi_dm_messages.posted_at
            # carries server_default '0' (migration 0020), so any writer that omits
            # it produces a tie group, and with LIMIT the tie makes row SELECTION
            # plan-dependent, not just row order.
            .order_by(PiDmMessage.posted_at.desc(), PiDmMessage.created_at.desc(),
                      PiDmMessage.id.desc())
            .limit(20)
        )
        dms = [
            {"direction": d.direction, "sender": d.sender_name or "", "content": d.content}
            for d in reversed(dm_rows.scalars().all())
        ]
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
            channels=channels, messages=messages, dms=dms,
            has_run=run_id is not None,
            posted=request.query_params.get("posted"),
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
        pi_may_reply_in_thread,
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

    reply_to = thread_ts.strip() or None
    if reply_to and not await pi_may_reply_in_thread(
        db,
        run_id=run_id,
        channel_name=target_channel,
        thread_ts=reply_to,
        agent_id=agent.agent_id,
    ):
        raise HTTPException(
            status_code=403,
            detail="Your agent is not a participant in that thread",
        )

    async def _write() -> None:
        await record_pi_message(
            db,
            run_id=run_id,
            channel_name=target_channel,
            content=text,
            sender_name=f"{current_user.name} (PI)",
            thread_ts=reply_to,
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
    # Form("") not Form(...): an emptied textarea submits `content=`, which Starlette's
    # form parser hands to FastAPI as a MISSING field, so a required parameter 422s and the
    # PI cannot clear their instructions at all — the clear path below (and the seed/file
    # clearing in #22 COR-23 / #29) only ran if they happened to leave whitespace behind.
    # Verified against a copy of production: `content=` -> 422 with nothing cleared.
    # But FastAPI's Form() dependency resolution collapses "submitted empty" and "field
    # omitted entirely" to the SAME default regardless of what that default is (verified:
    # Form(None) returns None for both cases too, not just Form("")) -- so distinguishing
    # them cannot be done via the Form() parameter at all. `form` below is the raw
    # Starlette FormData, which DOES tell them apart (a genuinely missing key is not `in`
    # it), matching the `"<field>" in form` presence-gating pattern save_public_profile
    # already uses for its six profile fields, just above.
    # The onboarding twin (onboarding.py::save_private_profile) has the same shape.
    content: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save private profile to disk and database."""
    form = await request.form()
    if "content" not in form:
        # #22 COR-23 residual (item 44): a request that OMITS `content`
        # entirely must not be treated the same as one that submits it empty
        # (which is a deliberate clear). Not reachable from the browser
        # (templates/agent/profile.html's textarea is always submitted), but a
        # data-destroying default for any other client.
        raise HTTPException(
            status_code=400,
            detail="content is required (send an empty string to clear the private profile)",
        )
    agent, is_owner = await get_agent_with_access(agent_id, db, current_user)
    if agent.status != "active":
        return RedirectResponse(url="/agent", status_code=302)

    # Persist to DB FIRST (COR-24: disk must never be ahead of the DB) — use the
    # PI's user_id, not the delegate's. Create the row if this is the first
    # private-profile save for this PI: `if profile:` used to make a missing row
    # a silent, permanent disk-only write with no DB record at all.
    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == agent.user_id)
    )
    profile = profile_result.scalar_one_or_none()
    if not profile:
        profile = ResearcherProfile(user_id=agent.user_id)
        db.add(profile)
    stripped = content.strip()
    profile.private_profile_md = stripped or None
    if not stripped:
        # A blank save must also clear the model-authored seed (matches
        # onboarding.py's save_private_profile) — otherwise a leftover seed
        # from an admin-seeded PI who never completed onboarding is
        # re-exported to disk by the next profile_pipeline run
        # (`content = md or seed`), undoing the clear (#22 COR-23, #29).
        profile.private_profile_seed = None
    await db.commit()

    # A blank/whitespace save deletes the exported file rather than writing
    # whitespace to it (#22 COR-23, #29) — agent.py's private_profile property
    # falls back to "No private instructions yet." only when the file is
    # absent, so a stale file left behind after clearing would keep the agent
    # honouring instructions the PI just deleted.
    profile_path = PROFILES_DIR / "private" / f"{agent.agent_id}.md"
    if stripped:
        profile_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(profile_path, content, encoding="utf-8")
    else:
        profile_path.unlink(missing_ok=True)

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
    form = await request.form()
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
        await db.flush()

    if "research_summary" in form:
        profile.research_summary = research_summary
    if "techniques" in form:
        profile.techniques = _parse_list(techniques)
    if "experimental_models" in form:
        profile.experimental_models = _parse_list(experimental_models)
    if "disease_areas" in form:
        profile.disease_areas = _parse_list(disease_areas)
    if "key_targets" in form:
        profile.key_targets = _parse_list(key_targets)
    if "keywords" in form:
        profile.keywords = _parse_list(keywords)
    profile.synthesis_validated = None
    profile.profile_version = await bump_profile_version(db, profile.id)

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

    user_email = current_user.email  # captured before the try: a failed guarded
    agent_row_id = agent.id  # statement expires attributes the except arm reads

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
            sid = await lookup_user_by_email_async(bot_token, user_email)
            if not sid:
                error = (
                    f"No Slack account found for {user_email}. "
                    "Please join the workspace first."
                )
            else:
                from src.services.delegate_slack_ids import append_delegate_slack_id_stmt

                await db.execute(append_delegate_slack_id_stmt(agent_row_id, sid))
                await db.commit()
                return RedirectResponse(
                    url=f"/agent/{agent_id}/dashboard", status_code=302
                )
    except Exception as exc:
        logger.warning("Delegate Slack lookup failed for %s: %s", user_email, exc)
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
        delegate_user_id = delegate.user_id  # locals captured BEFORE any guarded
        delegate_email = delegate.user.email  # db.execute() below
        agent_slug, agent_row_id = agent.agent_id, agent.id
        actor_name = current_user.name

        sid = None
        if delegate_email:
            # array_remove is a no-op when the id isn't present, so this must not be gated
            # on the in-memory `agent.delegate_slack_ids` hint: a concurrent append landing
            # in another session after this session's `agent` was loaded would leave that
            # hint stale (None/empty) while the DB row already holds the id, and skipping
            # the lookup here would let the removed delegate's Slack id keep agent-command
            # authority (src/agent/simulation.py ~4011-4021).
            from src.services.slack_tokens import get_any_bot_token

            bot_token = await get_any_bot_token(db)  # SQL — kept OUTSIDE the try below
            if bot_token:
                try:  # LOOKUP only — no SQL inside the best-effort try
                    from src.services.slack_web import lookup_user_by_email_async

                    sid = await lookup_user_by_email_async(bot_token, delegate_email)
                except Exception as exc:
                    logger.warning("Delegate Slack sync is best-effort; skipped: %s", exc)

        if sid:  # OUTSIDE the swallowing try — a failed UPDATE must not be hidden
            from src.services.delegate_slack_ids import remove_delegate_slack_id_stmt

            await db.execute(remove_delegate_slack_id_stmt(agent_row_id, sid))

        await db.delete(delegate)
        await db.commit()
        logger.info(
            "Delegate %s removed from agent %s by %s",
            delegate_user_id, agent_slug, actor_name,
        )

    return RedirectResponse(url=f"/agent/{agent_id}/dashboard", status_code=302)
