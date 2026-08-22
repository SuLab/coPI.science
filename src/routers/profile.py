"""Profile view and edit router."""

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_current_user, get_pi_user
from src.models import Job, Publication, ResearcherProfile, User
from src.services.profile_edit import apply_profile_edits

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")


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

    return templates.TemplateResponse(
        request,
        "profile/view.html",
        _template_context(
            request,
            current_user,
            profile=profile,
            publications=publications,
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
        name=name, email=email, institution=institution, department=department,
        research_summary=research_summary, techniques=techniques,
        experimental_models=experimental_models, disease_areas=disease_areas,
        key_targets=key_targets, keywords=keywords,
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
    job = Job(
        type="generate_profile",
        user_id=current_user.id,
        payload={"user_id": str(current_user.id), "orcid": current_user.orcid},
    )
    db.add(job)
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
    """Delete user account after confirmation."""
    if confirm.lower() != "delete":
        return RedirectResponse(url="/profile/delete-account?error=1", status_code=302)

    await db.delete(current_user)
    await db.commit()

    request.session.clear()
    response = RedirectResponse(url="/login?deleted=1", status_code=302)
    response.delete_cookie("copi-impersonate")
    return response
