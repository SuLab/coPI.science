"""Onboarding flow router."""

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_current_user
from src.models import AgentRegistry, Job, ResearcherProfile, User
from src.services.profile_export import (
    PRIVATE_PROFILES_DIR,
    export_private_profile,
)

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
        request,
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

    # Look up agent_id (gates file export and revision)
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == current_user.id)
    )
    agent_reg = agent_result.scalar_one_or_none()
    agent_id_for_export = agent_reg.agent_id if agent_reg else None

    # Export to markdown for agent consumption (include publications)
    from src.services.profile_export import export_profile_to_markdown
    from src.models import Publication
    pub_result = await db.execute(
        select(Publication).where(Publication.user_id == current_user.id)
    )
    user_pubs = list(pub_result.scalars().all())
    exported_path = export_profile_to_markdown(
        current_user, profile, agent_id_for_export, publications=user_pubs
    )

    # Record revision
    from src.services.profile_versioning import create_revision
    if agent_reg and exported_path:
        await create_revision(
            db,
            agent_registry_id=agent_reg.id,
            profile_type="public",
            content=exported_path.read_text(encoding="utf-8"),
            changed_by_user_id=current_user.id,
            mechanism="web",
            change_summary="Profile saved during onboarding",
        )
        await db.commit()

    return RedirectResponse(url="/onboarding/private-profile", status_code=302)


@router.get("/private-profile", response_class=HTMLResponse)
async def private_profile(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Step 4: review and edit seeded private profile."""
    if current_user.onboarding_complete:
        return RedirectResponse(url="/profile", status_code=302)

    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == current_user.id)
    )
    profile = profile_result.scalar_one_or_none()

    # Show the best available content: DB live profile → DB seed → on-disk file → default template
    content = ""
    if profile:
        content = profile.private_profile_md or profile.private_profile_seed or ""

    # Fall back to existing on-disk private profile (e.g. pilot labs that were
    # set up before the user claimed their account via ORCID login).
    if not content:
        agent_result = await db.execute(
            select(AgentRegistry).where(AgentRegistry.user_id == current_user.id)
        )
        agent_reg = agent_result.scalar_one_or_none()
        if agent_reg:
            disk_path = PRIVATE_PROFILES_DIR / f"{agent_reg.agent_id}.md"
            if disk_path.exists():
                content = disk_path.read_text(encoding="utf-8").strip()

    # For brand-new users with no existing profile anywhere, seed with the
    # standard section template so they aren't staring at a blank page.
    if not content:
        lab_name = current_user.name or "My"
        content = f"""# {lab_name} Lab — Private Profile

## PI Behavioral Instructions

### Collaboration Preferences
- Add preferences here: what kinds of collaborations interest you, and what would you rather not pursue?

### Communication Style
- Add guidance for how your agent should communicate on your behalf (e.g. tone, what to emphasize or avoid).

### Topic Priorities
- No specific priority ordering yet. Add priorities here to guide which opportunities your agent pursues first.

### Criteria to Always Explore
- No specific criteria yet. Add questions or checks your agent should always ask when evaluating collaborations."""

    return templates.TemplateResponse(
        request,
        "onboarding/private_profile.html",
        _template_context(request, current_user, profile=profile, profile_content=content),
    )


@router.post("/private-profile")
async def save_private_profile(
    request: Request,
    content: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save the private profile from onboarding step 4."""
    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == current_user.id)
    )
    profile = profile_result.scalar_one_or_none()
    if not profile:
        profile = ResearcherProfile(user_id=current_user.id)
        db.add(profile)

    profile.private_profile_md = content.strip() or None
    profile.private_profile_seed = None  # Clear seed after user saves

    # Mark onboarding complete
    current_user.onboarding_complete = True

    await db.commit()

    # Look up agent_id (gates file export and revision)
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == current_user.id)
    )
    agent_reg = agent_result.scalar_one_or_none()
    agent_id_for_export = agent_reg.agent_id if agent_reg else None

    # Export to disk
    export_private_profile(current_user, profile, agent_id_for_export)

    # Record revision
    from src.services.profile_versioning import create_revision
    if agent_reg and content.strip():
        await create_revision(
            db,
            agent_registry_id=agent_reg.id,
            profile_type="private",
            content=content.strip(),
            changed_by_user_id=current_user.id,
            mechanism="web",
            change_summary="Private profile saved during onboarding",
        )
        await db.commit()

    # Check for pending invite token
    pending_token = request.session.pop("pending_invite_token", None)
    if pending_token:
        return RedirectResponse(url=f"/invite/{pending_token}", status_code=302)

    return RedirectResponse(url="/profile?onboarding_complete=1", status_code=302)


@router.post("/complete")
async def complete_onboarding(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Mark onboarding as complete."""
    current_user.onboarding_complete = True
    await db.commit()

    pending_token = request.session.pop("pending_invite_token", None)
    if pending_token:
        return RedirectResponse(url=f"/invite/{pending_token}", status_code=302)

    return RedirectResponse(url="/profile?onboarding_complete=1", status_code=302)


@router.get("/done", response_class=HTMLResponse)
async def onboarding_done(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    return templates.TemplateResponse(
        request,
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
