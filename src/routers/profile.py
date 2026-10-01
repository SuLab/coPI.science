"""Profile view and edit router."""

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_current_user, get_pi_user, refuse_impersonation
from src.models import AgentRegistry, Publication, ResearcherProfile, User
from src.models.job import INTERACTIVE_PRIORITY
from src.services.admin_invariant import LastAdminError, ensure_admin_remains
from src.services.profile_edit import apply_profile_edits, parse_expected_version
from src.services.profile_jobs import enqueue_profile_job_if_absent
from src.services.tenure_scope import scoped_publications_for
from src.services.user_deletion import delete_user_account
from src.web.templating import make_templates

logger = logging.getLogger(__name__)
router = APIRouter()
templates = make_templates()


def _template_context(request: Request, user: User, **kwargs) -> dict:
    impersonated = getattr(user, "_is_impersonated", False)
    real_admin = getattr(user, "_real_admin", None)
    ctx = {
        "request": request,
        "current_user": real_admin if impersonated else user,
        "user": user,
        "impersonation_banner": user if impersonated else None,
        "active_page": "profile",
    }
    ctx.update(kwargs)
    return ctx


def _parse_list(val: str) -> list[str]:
    return [s.strip() for s in val.split(",") if s.strip()]


@router.get("", response_class=HTMLResponse)
async def profile_view(
    request: Request,
    onboarding_complete: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """View user's profile page."""
    # A REVIEWER is neither staff nor PI and has no lab profile to
    # view — bounce before the onboarding check, which would otherwise send
    # it to a page it can never complete (get_pi_user gates the only writer
    # of onboarding_complete).
    if current_user.is_reviewer:
        return RedirectResponse(url="/manager/assessments", status_code=302)

    # Redirect to onboarding if not complete
    if not current_user.onboarding_complete:
        return RedirectResponse(url="/onboarding", status_code=302)

    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == current_user.id)
    )
    profile = profile_result.scalar_one_or_none()

    pub_result = await db.execute(
        select(Publication)
        .where(Publication.user_id == current_user.id)
        .order_by(Publication.year.desc())
    )
    publications = pub_result.scalars().all()

    # Tenure-scope the count/list (D17 of
    # docs/plans/2026-09-22-pi-corpus-attribution-remediation-plan.md).
    # `current_user` (from `get_current_user`) does not eager-load `.agent`,
    # so the legacy agent-keyed tenure fallback needs its own small lookup
    # rather than a lazy load, which would raise outside a sync context.
    agent_id = (
        await db.execute(
            select(AgentRegistry.agent_id).where(
                AgentRegistry.user_id == current_user.id
            )
        )
    ).scalar_one_or_none()
    pub_scope = await scoped_publications_for(
        db, current_user.id, agent_id, publications=publications
    )

    return templates.TemplateResponse(
        request,
        "profile/view.html",
        _template_context(
            request,
            current_user,
            profile=profile,
            publications=pub_scope.publications,
            pub_scope=pub_scope,
            just_completed_onboarding=onboarding_complete,
        ),
    )


@router.get("/edit", response_class=HTMLResponse)
async def profile_edit(
    request: Request,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Edit profile page."""
    # Same reviewer bounce as GET /profile: without it a reviewer renders a
    # profile-edit form whose POST /profile/save 403s (get_pi_user).
    if current_user.is_reviewer:
        return RedirectResponse(url="/manager/assessments", status_code=302)

    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == current_user.id)
    )
    profile = profile_result.scalar_one_or_none()

    return templates.TemplateResponse(
        request,
        "profile/edit.html",
        _template_context(
            request, current_user, profile=profile,
            error=error,
        ),
    )


@router.post("/save")
async def profile_save(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    institution: str = Form(""),
    department: str = Form(""),
    research_summary: str = Form(""),
    techniques: str = Form(""),
    experimental_models: str = Form(""),
    disease_areas: str = Form(""),
    key_targets: str = Form(""),
    keywords: str = Form(""),
    profile_version: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_pi_user),
):
    """Save profile changes.

    get_pi_user, matching the four sibling PI writes (/profile/refresh,
    /onboarding/save-profile, /onboarding/retry, /agent/request). This one was
    left on get_current_user when the others were moved, so a manager could
    create a ResearcherProfile on their own account and — via
    apply_profile_edits — rewrite users.email, the field delegate-invitation
    acceptance binds to (E1.3). Managers keep POST
    /manager/pis/{user_id}/profile, which calls the same service function.
    """
    error = await apply_profile_edits(
        db, target_user=current_user, changed_by_user_id=current_user.id,
        form={
            "name": name, "email": email, "institution": institution,
            "department": department, "research_summary": research_summary,
            "techniques": techniques, "experimental_models": experimental_models,
            "disease_areas": disease_areas, "key_targets": key_targets,
            "keywords": keywords,
        },
        expected_version=parse_expected_version(profile_version),
    )
    if error:
        return RedirectResponse(url=f"/profile/edit?error={error}", status_code=302)
    return RedirectResponse(url="/profile?saved=1", status_code=302)


@router.post("/refresh")
async def profile_refresh(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_pi_user),
):
    """Enqueue a profile refresh job.

    get_pi_user: a manager has no research profile to refresh (D7), and this
    fires the same ORCID/PubMed generate_profile pipeline that F8 kept off
    manager accounts on the onboarding side.
    """
    await enqueue_profile_job_if_absent(db, current_user, priority=INTERACTIVE_PRIORITY)
    await db.commit()
    return RedirectResponse(url="/profile?refreshing=1", status_code=302)


@router.get("/delete-account", response_class=HTMLResponse)
async def delete_account_confirm(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Account deletion confirmation page."""
    return templates.TemplateResponse(
        request,
        "profile/delete_account.html",
        _template_context(request, current_user),
    )


@router.post("/delete-account")
async def delete_account(
    request: Request,
    confirm: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete the user's own account after confirmation.

    Refuses impersonated sessions: an admin viewing an account through
    impersonation is on a support path, and account deletion is the one
    action there that cannot be undone (2026-08-22 audit §2.13; deletion
    audit F8). Admins delete accounts through /admin/users/{id}/delete,
    which logs the actor.
    """
    refuse_impersonation(current_user, "Account deletion is disabled while impersonating.")

    if confirm.lower() != "delete":
        return RedirectResponse(url="/profile/delete-account?error=1", status_code=302)

    # The same "at least one admin can still log in" invariant the role
    # route defends (src/routers/admin/users.py) — deletion is the other door out
    # of adminhood, and it had no guard (deletion audit F7).
    try:
        await ensure_admin_remains(db, user=current_user)
    except LastAdminError:
        return RedirectResponse(url="/profile/delete-account?error=last_admin", status_code=302)

    await delete_user_account(db, current_user)

    request.session.clear()
    response = RedirectResponse(url="/login?deleted=1", status_code=302)
    response.delete_cookie("copi-impersonate")
    return response
