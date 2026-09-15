"""Profile view and edit router."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_current_user
from src.models import Job, Publication, ResearcherProfile, User
from src.services.account_deletion import agent_blocking_account_delete
from src.services.profile_pipeline import bump_profile_version
from src.services.validators import is_valid_email

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
            request, current_user, profile=profile, agent_registry=agent_reg,
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
    current_user: User = Depends(get_current_user),
):
    """Save profile changes."""
    # Which fields the client actually SENT. FastAPI maps an empty Form value to
    # the parameter default, so `Form(None)` cannot tell "the user cleared this
    # box" (must write "") from "the field was not in the POST at all" (must
    # leave the stored value alone). The raw form can.
    form = await request.form()

    # Only touch email if the client actually sent the field — same presence
    # gate as the profile fields below. A POST that omits `email` must not
    # silently NULL User.email (nullable+unique, so nothing would raise).
    if "email" in form:
        # Validate the email up front so a bad value rejects the whole submission
        # before anything is persisted.
        email_clean = (email or "").strip().lower()
        if email_clean != (current_user.email or ""):
            if email_clean:
                if not is_valid_email(email_clean):
                    return RedirectResponse(
                        url="/profile/edit?error=invalid_email", status_code=302
                    )
                existing = await db.execute(
                    select(User).where(
                        User.email == email_clean, User.id != current_user.id
                    )
                )
                if existing.scalar_one_or_none():
                    return RedirectResponse(
                        url="/profile/edit?error=email_taken", status_code=302
                    )
            current_user.email = email_clean or None

    # Update user fields
    if name:
        current_user.name = name
    if "institution" in form:
        current_user.institution = institution or None
    if "department" in form:
        current_user.department = department or None

    # Update profile fields
    profile_result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == current_user.id)
    )
    profile = profile_result.scalar_one_or_none()
    if not profile:
        profile = ResearcherProfile(user_id=current_user.id)
        db.add(profile)
        await db.flush()  # row must exist before the atomic version UPDATE below

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
    # A hand edit through the web UI is not a synthesis at all — None ("unknown /
    # not a synthesis") rather than False, so profile_pipeline.py's
    # stored_is_worth_keeping gate (`is not False`) protects it on the next run.
    profile.synthesis_validated = None
    profile.profile_version = await bump_profile_version(db, profile.id)

    await db.commit()

    # Look up agent_id (gates file export and revision)
    from src.models import AgentRegistry
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
    # Annotated rather than `= Depends(...)`: this handler needed a new `db`
    # dependency for the guard below, and the `= Depends(...)` spelling costs a
    # ruff B008 per parameter (11 of src/'s 251 findings are that rule in this
    # file). Same behaviour, no new debt on the SRC_LINT_MAX ratchet.
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Account deletion confirmation page.

    Renders the refusal instead of the confirm form when the caller owns an
    agent the delete would orphan, so the POST's 409 is never a surprise.
    """
    return templates.TemplateResponse(
        request,
        "profile/delete_account.html",
        _template_context(
            request,
            current_user,
            blocking_agent=await agent_blocking_account_delete(
                db, current_user, for_update=False,
            ),
        ),
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

    # agents.user_id is ondelete="SET NULL", so deleting the owner of a live
    # agent leaves status='active' with no owner — a bot on Slack in this PI's
    # name that nobody can deactivate, edit or answer proposals for. Refuse,
    # and say what to do about it, rather than silently deactivating an agent
    # the user did not ask us to touch.
    blocking_agent = await agent_blocking_account_delete(db, current_user)
    if blocking_agent is not None:
        return templates.TemplateResponse(
            request,
            "profile/delete_account.html",
            _template_context(request, current_user, blocking_agent=blocking_agent),
            status_code=409,
        )

    try:
        await db.delete(current_user)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Account could not be deleted due to a database conflict; please retry.",
        ) from exc

    request.session.clear()
    response = RedirectResponse(url="/login?deleted=1", status_code=302)
    response.delete_cookie("copi-impersonate")
    return response
