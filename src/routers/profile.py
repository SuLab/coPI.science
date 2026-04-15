"""Profile view and edit router."""

import logging

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
            pending_profile=profile.pending_profile if profile else None,
            just_completed_onboarding=onboarding_complete,
        ),
    )


@router.get("/edit", response_class=HTMLResponse)
async def profile_edit(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Edit profile page."""
    from src.models import AgentRegistry

    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == current_user.id)
    )
    profile = profile_result.scalar_one_or_none()

    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == current_user.id)
    )
    agent_reg = agent_result.scalar_one_or_none()

    return templates.TemplateResponse(
        request,
        "profile/edit.html",
        _template_context(
            request, current_user, profile=profile, agent_registry=agent_reg
        ),
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

    profile.research_summary = research_summary
    profile.techniques = _parse_list(techniques)
    profile.experimental_models = _parse_list(experimental_models)
    profile.disease_areas = _parse_list(disease_areas)
    profile.key_targets = _parse_list(key_targets)
    profile.keywords = _parse_list(keywords)
    profile.profile_version = (profile.profile_version or 0) + 1

    await db.commit()

    # Export to markdown for agent consumption (include publications)
    from src.services.profile_export import export_profile_to_markdown
    from src.models import Publication
    pub_result = await db.execute(
        select(Publication).where(Publication.user_id == current_user.id)
    )
    user_pubs = list(pub_result.scalars().all())
    exported_path = export_profile_to_markdown(current_user, profile, publications=user_pubs)

    # Record revision
    from src.services.profile_versioning import create_revision
    from src.models import AgentRegistry
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == current_user.id)
    )
    agent_reg = agent_result.scalar_one_or_none()
    if agent_reg and exported_path:
        await create_revision(
            db,
            agent_registry_id=agent_reg.id,
            profile_type="public",
            content=exported_path.read_text(encoding="utf-8"),
            changed_by_user_id=current_user.id,
            mechanism="web",
        )
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
