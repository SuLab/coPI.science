"""Onboarding flow router."""

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_current_user, get_pi_user
from src.models import AgentRegistry, Job, ResearcherProfile, User
from src.routers.auth import pop_post_login_redirect
from src.services.validators import is_valid_email

logger = logging.getLogger(__name__)
router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _maybe_send_welcome(user: User, was_complete: bool) -> None:
    """Send the welcome email once, the first time a user completes onboarding.

    No-op if onboarding was already complete or the user has no email. The send
    catches its own errors, so a failure never blocks onboarding completion.
    """
    if was_complete or not user.email:
        return
    from src.services.email import send_welcome_email
    send_welcome_email(user.email, name=user.name, user_id=str(user.id))


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
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Main onboarding page — shows profile review."""
    if current_user.onboarding_complete:
        return RedirectResponse(url="/profile", status_code=302)
    # A MANAGER has no research profile to review (D7). Bounce it rather than
    # render a PI page it can never complete.
    #
    # Deliberately is_manager, not `!= USER_ROLE_PI`. An admin is not a `pi`
    # either, but admins keep the PI surfaces: templates/base.html still shows
    # them My Profile / My Agent, and /profile bounces anyone whose onboarding
    # is incomplete straight back here. A `!= 'pi'` test therefore trapped an
    # admin with onboarding_complete=False in a permanent
    # /profile -> /onboarding -> /manager/pis deflection with no way to ever
    # finish onboarding — locked out of their own profile by a guard aimed at
    # managers.
    if current_user.is_manager:
        return RedirectResponse(url="/manager/pis", status_code=302)

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

    # Self-heal: an allowed user with no job and no profile would otherwise
    # spin on "Building Your Profile" forever (the template treats job_status
    # 'none' the same as pending/processing and offers no retry).
    #
    # `not is_manager` for the same reason as the bounce above: F8's concern is
    # firing ORCID/PubMed profile generation for an account that has no lab and
    # may have no relevant publications, which is a MANAGER. Narrowing this to
    # `== 'pi'` would leave an admin staring at "Building Your Profile" with no
    # job, no profile and no retry — the exact spin this self-heal exists to
    # prevent.
    if (
        job is None
        and profile is None
        and current_user.access_status == "allowed"
        and not current_user.is_manager
    ):
        job = Job(
            type="generate_profile",
            user_id=current_user.id,
            payload={"user_id": str(current_user.id), "orcid": current_user.orcid},
        )
        db.add(job)
        await db.commit()
        logger.info("Auto-enqueued generate_profile for user %s on /onboarding", current_user.id)

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
            error=error,
        ),
    )


@router.post("/save-profile")
async def save_profile(
    request: Request,
    email: str = Form(""),
    research_summary: str = Form(""),
    techniques: str = Form(""),
    experimental_models: str = Form(""),
    disease_areas: str = Form(""),
    key_targets: str = Form(""),
    keywords: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_pi_user),
):
    """Save profile edits from onboarding.

    get_pi_user, not get_current_user: this is the only writer of
    `onboarding_complete = True` in src/ and it also creates the
    ResearcherProfile, which together are the entire gate on
    POST /agent/request. A manager reaching it would be two POSTs from a lab
    bot of its own (D7).
    """

    # Email is required at onboarding. Validate before persisting anything so a
    # bad value rejects the whole submission (mirrors profile_save on /profile).
    email_clean = (email or "").strip().lower()
    if not email_clean:
        return RedirectResponse(url="/onboarding?error=email_required", status_code=302)
    if not is_valid_email(email_clean):
        return RedirectResponse(url="/onboarding?error=invalid_email", status_code=302)
    if email_clean != (current_user.email or ""):
        existing = await db.execute(
            select(User).where(User.email == email_clean, User.id != current_user.id)
        )
        if existing.scalar_one_or_none():
            return RedirectResponse(url="/onboarding?error=email_taken", status_code=302)
        current_user.email = email_clean

    def parse_list(val: str) -> list[str]:
        return [s.strip() for s in val.split(",") if s.strip()]

    result = await db.execute(
        select(ResearcherProfile).where(ResearcherProfile.user_id == current_user.id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        profile = ResearcherProfile(user_id=current_user.id)
        db.add(profile)
        # Flush the row into existence before the SQL-side bump below: on a
        # pending object the expression would render inside the INSERT's VALUES,
        # which cannot reference its own target table ("invalid reference to
        # FROM-clause entry for table researcher_profiles").
        await db.flush()

    profile.research_summary = research_summary
    profile.techniques = parse_list(techniques)
    profile.experimental_models = parse_list(experimental_models)
    profile.disease_areas = parse_list(disease_areas)
    profile.key_targets = parse_list(key_targets)
    profile.keywords = parse_list(keywords)
    # SQL-side increment: the Python read-modify-write lost updates when two
    # writers raced (issue #22 C1). Nothing below reads profile_version, so the
    # expiry the expression assignment causes needs no refresh here.
    profile.profile_version = func.coalesce(ResearcherProfile.profile_version, 0) + 1

    await db.commit()

    # Look up agent_id (gates file export and revision)
    agent_result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.user_id == current_user.id)
    )
    agent_reg = agent_result.scalar_one_or_none()
    agent_id_for_export = agent_reg.agent_id if agent_reg else None

    # Export to markdown for agent consumption (include publications)
    from src.models import Publication
    from src.services.profile_export import export_profile_to_markdown
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

    # This is now the terminal step of onboarding (the private-profile step
    # that used to own completion — onboarding_complete flip, welcome email,
    # pending-invite/post-login-redirect resume — was removed with private
    # instructions; those side effects relocate here). Not gated on
    # `was_complete` alone being new: the guard on `_maybe_send_welcome`
    # itself still makes a replay of this POST a no-op for the welcome email.
    was_complete = current_user.onboarding_complete
    current_user.onboarding_complete = True
    await db.commit()

    _maybe_send_welcome(current_user, was_complete)

    # Check for pending invite token
    pending_token = request.session.pop("pending_invite_token", None)
    if pending_token:
        request.session.pop("post_login_redirect", None)
        return RedirectResponse(url=f"/invite/{pending_token}", status_code=302)

    # Resume the page the user originally requested before being sent to login.
    next_url = pop_post_login_redirect(request)
    if next_url:
        return RedirectResponse(url=next_url, status_code=302)
    return RedirectResponse(url="/profile?onboarding_complete=1", status_code=302)


@router.post("/retry")
async def retry_pipeline(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_pi_user),
):
    """Re-enqueue profile generation job.

    POST-only: this creates and commits a Job, so over GET it was a
    cross-site request-forgery target (a forged navigation could enqueue work
    on the victim's behalf). SameSite=lax on the session cookie blocks forged
    cross-site POSTs, so the "Try Again" control posts this form. (SEC-8)

    Gated on get_pi_user: this is the POST twin of the GET self-heal at
    ``onboarding_start``, which already refuses to enqueue generate_profile
    for a manager (F8). Narrowing only the GET left the pipeline one form
    POST away.
    """
    job = Job(
        type="generate_profile",
        user_id=current_user.id,
        payload={"user_id": str(current_user.id), "orcid": current_user.orcid},
    )
    db.add(job)
    await db.commit()
    return RedirectResponse(url="/onboarding", status_code=302)
