"""Profile view and edit router."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_current_user
from src.models import Job, Publication, ResearcherProfile, User

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
        "profile/view.html",
        _template_context(
            request,
            current_user,
            profile=profile,
            publications=publications,
            pending_profile=profile.pending_profile if profile else None,
        ),
    )


@router.get("/edit", response_class=HTMLResponse)
async def profile_edit(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Edit profile page."""
    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == current_user.id)
    )
    profile = profile_result.scalar_one_or_none()

    return templates.TemplateResponse(
        "profile/edit.html",
        _template_context(request, current_user, profile=profile),
    )


@router.post("/save")
async def profile_save(
    request: Request,
    name: str = Form(""),
    institution: str = Form(""),
    department: str = Form(""),
    research_summary: str = Form(""),
    techniques: str = Form(""),
    experimental_models: str = Form(""),
    disease_areas: str = Form(""),
    key_targets: str = Form(""),
    keywords: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save profile changes."""
    # Update user fields
    if name:
        current_user.name = name
    if institution is not None:
        current_user.institution = institution or None
    if department is not None:
        current_user.department = department or None

    # Update profile fields
    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == current_user.id)
    )
    profile = profile_result.scalar_one_or_none()
    if not profile:
        profile = ResearcherProfile(user_id=current_user.id)
        db.add(profile)

    if research_summary:
        profile.research_summary = research_summary
    if techniques:
        profile.techniques = _parse_list(techniques)
    if experimental_models:
        profile.experimental_models = _parse_list(experimental_models)
    if disease_areas:
        profile.disease_areas = _parse_list(disease_areas)
    if key_targets:
        profile.key_targets = _parse_list(key_targets)
    if keywords:
        profile.keywords = _parse_list(keywords)
    profile.profile_version = (profile.profile_version or 0) + 1

    await db.commit()
    return RedirectResponse(url="/profile?saved=1", status_code=302)


@router.post("/refresh")
async def profile_refresh(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Enqueue a profile refresh job."""
    job = Job(
        type="generate_profile",
        user_id=current_user.id,
        payload={"user_id": str(current_user.id), "orcid": current_user.orcid},
    )
    db.add(job)
    await db.commit()
    return RedirectResponse(url="/profile?refreshing=1", status_code=302)


@router.get("/add-text", response_class=HTMLResponse)
async def add_text_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Page to add a user-submitted text block."""
    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == current_user.id)
    )
    profile = profile_result.scalar_one_or_none()

    return templates.TemplateResponse(
        "profile/add_text.html",
        _template_context(request, current_user, profile=profile),
    )


@router.post("/add-text")
async def add_text_submit(
    request: Request,
    label: str = Form(...),
    content: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Submit a new user text block."""
    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == current_user.id)
    )
    profile = profile_result.scalar_one_or_none()
    if not profile:
        profile = ResearcherProfile(user_id=current_user.id)
        db.add(profile)

    texts = list(profile.user_submitted_texts or [])
    if len(texts) >= 5:
        return RedirectResponse(url="/profile/edit?error=max_texts", status_code=302)

    # Cap content at 2000 words
    words = content.split()
    if len(words) > 2000:
        content = " ".join(words[:2000])

    texts.append({
        "label": label[:100],
        "content": content,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    })
    profile.user_submitted_texts = texts

    # Enqueue re-synthesis job
    job = Job(
        type="generate_profile",
        user_id=current_user.id,
        payload={
            "user_id": str(current_user.id),
            "orcid": current_user.orcid,
            "reason": "user_text_added",
        },
    )
    db.add(job)
    await db.commit()
    return RedirectResponse(url="/profile/edit?text_added=1", status_code=302)


@router.get("/delete-text/{index}")
async def delete_text(
    index: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a user-submitted text block by index."""
    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == current_user.id)
    )
    profile = profile_result.scalar_one_or_none()
    if profile and profile.user_submitted_texts:
        texts = list(profile.user_submitted_texts)
        if 0 <= index < len(texts):
            texts.pop(index)
            profile.user_submitted_texts = texts
            await db.commit()
    return RedirectResponse(url="/profile/edit", status_code=302)


@router.get("/delete-account", response_class=HTMLResponse)
async def delete_account_confirm(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Account deletion confirmation page."""
    return templates.TemplateResponse(
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
