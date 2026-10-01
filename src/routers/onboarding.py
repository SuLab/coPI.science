"""Onboarding flow router."""

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_current_user, get_pi_user
from src.models import Job, ResearcherProfile, User
from src.models.job import INTERACTIVE_PRIORITY
from src.routers.auth import pop_post_login_redirect
from src.services.email import build_welcome, send_transactional_email
from src.services.profile_edit import apply_profile_edits, parse_expected_version
from src.services.profile_jobs import enqueue_profile_job_if_absent
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
    # The rule is `may_use_pi_surfaces` (PI or admin), not `!= USER_ROLE_PI`:
    # admins keep the PI surfaces (templates/base.html shows them My Profile /
    # My Agent, and /profile bounces anyone whose onboarding is incomplete
    # straight back here), so a `!= 'pi'` test would trap an admin in a
    # permanent /profile -> /onboarding -> /manager/pis deflection.
    # A REVIEWER is neither staff nor PI and has no research profile either;
    # each role keeps its own landing page.
    if not current_user.may_use_pi_surfaces:
        if current_user.is_manager:
            return RedirectResponse(url="/manager/pis", status_code=302)
        return RedirectResponse(url="/manager/assessments", status_code=302)

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
    # `may_use_pi_surfaces` for the same reason as the bounce above: F8's
    # concern is firing ORCID/PubMed profile generation for an account that has
    # no lab and may have no relevant publications, which is a MANAGER.
    # Narrowing this to `== 'pi'` would leave an admin staring at "Building Your
    # Profile" with no job, no profile and no retry.
    if (
        job is None
        and profile is None
        and current_user.access_status == "allowed"
        and current_user.may_use_pi_surfaces
    ):
        job = await enqueue_profile_job_if_absent(db, current_user, priority=INTERACTIVE_PRIORITY)
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
    profile_version: str = Form(""),
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

    # Email is required at onboarding. apply_profile_edits validates it before
    # persisting anything, so a bad value rejects the whole submission.
    error = await apply_profile_edits(
        db, target_user=current_user, changed_by_user_id=current_user.id,
        form={
            "email": email, "research_summary": research_summary,
            "techniques": techniques, "experimental_models": experimental_models,
            "disease_areas": disease_areas, "key_targets": key_targets,
            "keywords": keywords,
        },
        expected_version=parse_expected_version(profile_version),
        email_required=True,
        change_summary="Profile saved during onboarding",
    )
    if error:
        return RedirectResponse(url=f"/onboarding?error={error}", status_code=302)

    # This is now the terminal step of onboarding (the private-profile step
    # that used to own completion — onboarding_complete flip, welcome email,
    # pending-invite/post-login-redirect resume — was removed with private
    # instructions; those side effects relocate here). The conditional UPDATE is
    # the once-only gate for the welcome email (PS-12): a replayed or concurrent
    # save matches no row. The email leaves only after the commit.
    flipped = (await db.execute(
        update(User).where(User.id == current_user.id, User.onboarding_complete.is_(False))
        .values(onboarding_complete=True).returning(User.id)
    )).scalar_one_or_none()
    await db.commit()
    if flipped is not None and current_user.email:
        await send_transactional_email(build_welcome(current_user.email, current_user.name))

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
    await enqueue_profile_job_if_absent(db, current_user, priority=INTERACTIVE_PRIORITY)
    await db.commit()
    return RedirectResponse(url="/onboarding", status_code=302)
