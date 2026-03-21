"""Onboarding flow router."""

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_current_user
from src.models import Job, ResearcherProfile, User

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _template_context(request: Request, user: User, **kwargs) -> dict:
    impersonated = getattr(user, "_is_impersonated", False)
    real_admin = getattr(user, "_real_admin", None)
    ctx = {
        "request": request,
        "current_user": real_admin if impersonated else user,
        "impersonation_banner": user if impersonated else None,
        "active_page": "onboarding",
    }
    ctx.update(kwargs)
    return ctx


@router.get("", response_class=HTMLResponse)
async def onboarding_start(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Main onboarding page — shows profile review."""
    if current_user.onboarding_complete:
        return RedirectResponse(url="/profile", status_code=302)

    # Get latest job for this user
    result = await db.execute(
        select(Job)
        .where(Job.user_id == current_user.id, Job.type == "generate_profile")
        .order_by(Job.enqueued_at.desc())
    )
    job = result.scalars().first()

    # Get profile
    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == current_user.id)
    )
    profile = profile_result.scalar_one_or_none()

    job_status = job.status if job else "none"
    progress = (job.payload or {}).get("progress", []) if job else []

    return templates.TemplateResponse(
        "onboarding/profile_review.html",
        _template_context(
            request,
            current_user,
            profile=profile,
            job=job,
            job_status=job_status,
            progress=progress,
        ),
    )


@router.post("/save-profile")
async def save_profile(
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
    """Save profile edits from onboarding."""

    def parse_list(val: str) -> list[str]:
        return [s.strip() for s in val.split(",") if s.strip()]

    result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == current_user.id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        profile = ResearcherProfile(user_id=current_user.id)
        db.add(profile)

    profile.research_summary = research_summary
    profile.techniques = parse_list(techniques)
    profile.experimental_models = parse_list(experimental_models)
    profile.disease_areas = parse_list(disease_areas)
    profile.key_targets = parse_list(key_targets)
    profile.keywords = parse_list(keywords)
    profile.profile_version = (profile.profile_version or 0) + 1

    await db.commit()
    return RedirectResponse(url="/onboarding/add-texts", status_code=302)


@router.get("/add-texts", response_class=HTMLResponse)
async def add_texts(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Step 4: add user-submitted texts."""
    if current_user.onboarding_complete:
        return RedirectResponse(url="/profile", status_code=302)

    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == current_user.id)
    )
    profile = profile_result.scalar_one_or_none()

    return templates.TemplateResponse(
        "onboarding/add_texts.html",
        _template_context(request, current_user, profile=profile),
    )


@router.post("/complete")
async def complete_onboarding(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark onboarding as complete."""
    current_user.onboarding_complete = True
    await db.commit()
    return RedirectResponse(url="/onboarding/done", status_code=302)


@router.get("/done", response_class=HTMLResponse)
async def onboarding_done(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return templates.TemplateResponse(
        "onboarding/complete.html",
        _template_context(request, current_user),
    )


@router.get("/retry")
async def retry_pipeline(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Re-enqueue profile generation job."""
    job = Job(
        type="generate_profile",
        user_id=current_user.id,
        payload={"user_id": str(current_user.id), "orcid": current_user.orcid},
    )
    db.add(job)
    await db.commit()
    return RedirectResponse(url="/onboarding", status_code=302)
