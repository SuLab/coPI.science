"""Canonical PI directory and its two staff write endpoints."""

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_review_user, refuse_impersonation
from src.models import USER_ROLE_PI, User
from src.models.pi_company import PI_COMPANY_ROLES
from src.services import directory, profile_review
from src.services.agent_activation import activation_blockers, persona_blockers
from src.services.company_discovery import latest_discovery_job
from src.services.email_verification import mark_email_verified
from src.services.jhu_rules import get_tenure_start
from src.services.persona_lifecycle import export_after_lifecycle
from src.services.pi_companies import PI_COMPANY_ROLE_LABELS, list_companies
from src.services.pi_onboarding import (
    OrcidNameRequired,
    adopt_agentless_pi,
    create_pending_agent_for,
    find_or_create_pi_by_orcid,
    validate_orcid,
)
from src.services.profile_jobs import profile_retry_warranted
from src.services.revision_history import list_public_revisions
from src.services.slack_tokens import token_for_agent_row
from src.web.flash import flash
from src.web.page_context import workspace_context
from src.web.templating import make_templates
from src.web.urls import page_url

from ._pi_common import _STAFF, _company_view, _create_pi_error_code, _discovery_view, logger

router = APIRouter()
templates = make_templates()
_DB = Depends(get_db)
_REVIEW = Depends(get_review_user)


@router.get("/pis", response_class=HTMLResponse, name="workspace_pis")
async def workspace_pis(
    request: Request,
    status_filter: str | None = None,
    institution_filter: str | None = None,
    claimed_filter: str | None = None,
    page: int = Query(1, ge=1, le=directory.MAX_PAGE),
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    """Research directory; reviewer requests deliberately ignore staff filters."""
    staff = current_user.is_staff
    filters = {
        "institution_filter": institution_filter,
        "roles": (USER_ROLE_PI,),
        "status_filter": status_filter if staff else None,
        "claimed_filter": claimed_filter if staff else None,
    }
    user_total = await directory.count_pi_directory(db, research_only=not staff, **filters)
    page_count = max(1, -(-user_total // directory.PI_DIRECTORY_PAGE_SIZE))
    page = min(page, page_count)
    user_data = await directory.list_pi_directory(db, page=page, research_only=not staff, **filters)
    return templates.TemplateResponse(
        request,
        "workspace/pis.html",
        workspace_context(
            request,
            current_user,
            section="pis",
            user_data=user_data,
            institution_filter=institution_filter,
            status_filter=status_filter if staff else None,
            claimed_filter=claimed_filter if staff else None,
            user_total=user_total,
            page=page,
            page_count=page_count,
        ),
    )


@router.get("/pis/{user_id}", response_class=HTMLResponse, name="workspace_pi_detail")
async def workspace_pi_detail(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _REVIEW,
):
    """Canonical PI research record; operational additions are staff-only."""
    staff = current_user.is_staff
    detail = await directory.load_pi_detail(db, user_id, staff=staff)
    if detail is None:
        raise HTTPException(status_code=404, detail="PI not found")
    target = detail["user"]
    agent = target.agent
    tenure_start = await get_tenure_start(db, user_id, agent_id=agent.agent_id if agent else None)
    companies = await list_companies(db, user_id)
    confirmed = [_company_view(row) for row in companies if row.status == "confirmed"]
    suggested = (
        [_company_view(row) for row in companies if row.status == "suggested"] if staff else []
    )
    blocked = request.query_params.get("activation_blocked") if staff else None
    blockers = (
        await activation_blockers(db, agent) + await persona_blockers(db, agent)
        if blocked and agent is not None
        else []
    )
    return templates.TemplateResponse(
        request,
        "workspace/pi_detail.html",
        workspace_context(
            request,
            current_user,
            section="pis",
            valid_user_roles=("pi", "manager", "reviewer", "admin"),
            target_user=target,
            profile=detail["profile"],
            publications=detail["publications"],
            pub_scope=detail["pub_scope"],
            grants=detail["grants"],
            grant_identity=detail["grant_identity"],
            orcid_fundings=detail["orcid_fundings"],
            grant_sections=detail["grant_sections"],
            industry=detail["industry"],
            industry_evidence=detail["industry_evidence"],
            tenure_start=tenure_start,
            jobs=detail.get("jobs", []),
            has_bot_token=staff and bool(agent is not None and token_for_agent_row(agent)),
            can_retry_profile=staff
            and profile_retry_warranted(
                detail["profile"],
                next(
                    (job for job in detail.get("jobs", []) if job.type == "generate_profile"), None
                ),
            ),
            activation_blocked=blocked,
            activated=request.query_params.get("activated") if staff else None,
            blockers=blockers,
            companies_confirmed=confirmed,
            companies_suggested=suggested,
            company_roles=PI_COMPANY_ROLES,
            company_role_labels=PI_COMPANY_ROLE_LABELS,
            discovery=_discovery_view(await latest_discovery_job(db, user_id)) if staff else None,
            review=await profile_review.load_review_cards(db, user_id) if staff else None,
            revisions=await list_public_revisions(db, user_id) if staff else None,
            tenure_provisional=detail["grant_sections"].tenure_start
            if tenure_start is None
            else None,
        ),
    )


@router.post("/pis")
async def workspace_create_pi(
    request: Request,
    orcid: str = Form(...),
    name: str = Form(""),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Create a PI via the ORCID pipeline (design D5/D6) — no manual profile
    form exists anywhere in the app; every profile is ORCID/publication
    derived. Rejects if the ORCID already belongs to anyone, any role.

    2026-08-24 auto-flow: one POST creates the User, the generate_profile
    Job, a PENDING (inert) AgentRegistry row and the employment-derived JHU
    tenure entry, in ONE commit — so the worker can never claim the job
    before the agent row exists (the ordering gap that silently skipped
    exports/revisions for seeded PIs). The pending row is provisioned and activated in a
    separate step (/admin/agents, or this router's /slack/provision and
    /activate routes), never here (D7: the row belongs to the new PI, never
    the manager).

    An ORCID already held by a PI account with no lab agent (a PI who signed in
    before being added) is adopted rather than refused: the agent row is minted
    for that account (``adopt_agentless_pi``). Every refusal rolls the whole
    attempt back: ``get_db`` commits on a clean return, so a redirect after a
    partial write would otherwise persist it.

    When ORCID has no public name the form must carry ``name`` (spec 2026-10-05 §6.3);
    a name outside the D60 allowlist answers ``invalid_name``.

    After the commit the persona is published
    (``persona_lifecycle.export_after_lifecycle``); a brand-new PI has no profile yet, so
    its file is written by the profile job."""
    adopted = False
    entered_name = name.strip() or None
    try:
        pi = await adopt_agentless_pi(db, orcid, name=entered_name)
        adopted = pi is not None
        if pi is None:
            pi = await find_or_create_pi_by_orcid(db, orcid, name=entered_name)
        pi_id = pi.id
        await create_pending_agent_for(db, pi)
        await db.commit()
    except OrcidNameRequired as exc:
        await db.rollback()
        return RedirectResponse(url=_name_required_url(request, exc.orcid), status_code=302)
    except ValueError as exc:
        await db.rollback()
        return RedirectResponse(
            url=page_url(request, "workspace_pis", query={"error": _create_pi_error_code(exc)}),
            status_code=302,
        )
    except RuntimeError:
        # derive_agent_identity ran out of numeric suffixes for this surname.
        await db.rollback()
        logger.exception(
            "Add-PI could not derive an agent identity for ORCID %r", orcid.strip()[:40]
        )
        return RedirectResponse(
            url=page_url(request, "workspace_pis", query={"error": "create_failed"}),
            status_code=302,
        )
    except IntegrityError as exc:
        # Two managers adding same-surname PIs can race the identity
        # derivation's SELECT-then-INSERT; the loser rolls the WHOLE creation
        # back (User + Job + agent together — the atomicity is the feature).
        await db.rollback()
        if "users_orcid_key" in str(exc.orig):
            # Two adds of the SAME ORCID raced past the existence check (D-17):
            # that is "already exists", not an agent-identity clash.
            return RedirectResponse(
                url=page_url(request, "workspace_pis", query={"error": "exists"}), status_code=302
            )
        logger.warning(
            "Add-PI race on agent identity for ORCID %r; rolled back",
            orcid.strip()[:40],
        )
        return RedirectResponse(
            url=page_url(request, "workspace_pis", query={"error": "agent_conflict"}),
            status_code=302,
        )
    # The new lab's persona after the commit (spec 2026-10-05 §6.4, D31): written when the
    # adopted account already has a profile; a file left at the slug is archived either way.
    await export_after_lifecycle(
        db,
        pi_id,
        event="Agent created (Add-PI)",
        actor_id=current_user.id,
        replace_leftover=True,
    )
    if adopted:
        flash(
            request,
            "This PI already had an account (they signed in with ORCID); "
            "their lab agent has been created.",
            "success",
        )
    return RedirectResponse(
        url=page_url(request, "workspace_pi_detail", user_id=pi_id), status_code=302
    )


def _name_required_url(request: Request, orcid: str) -> str:
    """The Add-PI form again, asking for the name, with the ORCID iD refilled when it is a
    valid one (never an unvalidated string in the Location)."""
    query = {"error": "name_required"}
    try:
        query["orcid"] = validate_orcid(orcid)
    except ValueError:
        pass
    return page_url(request, "workspace_pis", query=query)


@router.post("/pis/{user_id}/verify-email")
async def workspace_verify_pi_email(
    user_id: uuid.UUID,
    request: Request,
    email: str = Form(""),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Mark a PI's email address verified (spec 2026-10-01 §6.6).

    PI targets only: staff and reviewer addresses are verified by an admin on
    /admin/users/{id}. Refused under impersonation, so the audit event names the
    staff member who actually vouched for the address — one of the few manager
    controls hidden while impersonating.
    """
    refuse_impersonation(current_user, "Email verification is disabled while impersonating.")
    target = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if target is None or target.user_role != USER_ROLE_PI:
        raise HTTPException(status_code=404, detail="PI not found")
    error = await mark_email_verified(db, target=target, actor=current_user, shown_email=email)
    if error:
        return RedirectResponse(
            url=page_url(request, "workspace_pi_detail", user_id=user_id, query={"error": error}),
            status_code=302,
        )
    logger.info("Staff user %s verified the email address of PI %s", current_user.id, user_id)
    return RedirectResponse(
        url=page_url(
            request, "workspace_pi_detail", user_id=user_id, query={"email_verified": "1"}
        ),
        status_code=302,
    )


#: ``SET LOCAL lock_timeout`` for the ORCID veto (spec 2026-10-05 §6.1). An ORCID refresh
#: holds the PI's funding rows only for its short store transaction, but a profile pipeline
#: holds the PI's persona lock from tenure derivation until its job commits (minutes, LLM
