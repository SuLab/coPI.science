"""Canonical PI profile and corpus-review write registrations."""

import uuid

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.dependencies import impersonation_note
from src.models import USER_ROLE_PI, Job, PublicationCandidate, ResearcherProfile, User
from src.models.job import INTERACTIVE_PRIORITY
from src.services import directory, profile_review
from src.services.profile_edit import (
    apply_profile_edits,
    list_fields_from_form,
    parse_expected_version,
)
from src.services.profile_jobs import enqueue_profile_job_if_absent, profile_retry_warranted
from src.services.profile_publish import reexport_persona, write_persona_files
from src.services.pubmed import fetch_pubmed_records
from src.web.flash import flash
from src.web.urls import page_url

from ._pi_common import (
    _DB,
    _STAFF,
    PERSONA_LOCK_TIMEOUT,
    _bounded_persona_locks,
    _PersonaBusy,
    _require_pi,
    _warn_if_impersonated,
    logger,
)

router = APIRouter()


@router.post("/pis/{user_id}/profile")
async def workspace_edit_pi_profile(
    user_id: uuid.UUID,
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    institution: str = Form(""),
    department: str = Form(""),
    research_summary: str | None = Form(None),
    jhu_tenure_start: str = Form(""),
    profile_version: str = Form(""),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Edit a PI's profile fields (design D8) — same fields as the PI's own
    /profile/save, attributed to the acting manager via changed_by_user_id,
    plus the JHU tenure-start year (manager-only field; blank = unchanged)."""
    detail = await directory.load_pi_target(db, user_id)
    if detail is None or detail["user"].user_role != USER_ROLE_PI:
        raise HTTPException(status_code=404, detail="PI not found")

    note = impersonation_note(current_user)
    error = await apply_profile_edits(
        db,
        target_user=detail["user"],
        changed_by_user_id=current_user.id,
        form={
            "name": name,
            "email": email,
            "institution": institution,
            "department": department,
            "research_summary": research_summary,
            **list_fields_from_form(await request.form()),
        },
        jhu_tenure_start=jhu_tenure_start,
        expected_version=parse_expected_version(profile_version),
        change_summary=note,
        mechanism="web_impersonated" if note else "web",
    )
    if error:
        return RedirectResponse(
            url=page_url(request, "workspace_pi_detail", user_id=user_id, query={"error": error}),
            status_code=302,
        )
    flash(request, "Profile saved.", "success")
    return RedirectResponse(
        url=page_url(request, "workspace_pi_detail", user_id=user_id), status_code=302
    )


@router.post("/pis/{user_id}/profile/retry")
async def workspace_retry_profile(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Queue this PI's profile generation again (F2 of the 2026-10-05 Add-PI audit).

    Before this route, a dead generation job or an ungrounded profile left a manager
    nothing to do but ask an admin to impersonate the PI and press Try Again — the
    activation gate refuses both states (and an empty summary, spec 2026-10-05 §6.4).
    Allowed only in those states
    (``profile_retry_warranted``), and the enqueue is the shared idempotent one, so
    a double click runs one pipeline."""
    target = await _require_pi(db, user_id)
    profile = (
        await db.execute(select(ResearcherProfile).where(ResearcherProfile.user_id == user_id))
    ).scalar_one_or_none()
    latest_job = (
        await db.execute(
            select(Job)
            .where(Job.user_id == user_id, Job.type == "generate_profile")
            .order_by(Job.enqueued_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not profile_retry_warranted(profile, latest_job):
        flash(
            request,
            "Nothing to retry: profile generation is running, or the profile is "
            "already grounded and has a summary.",
            "error",
        )
        return RedirectResponse(
            url=page_url(request, "workspace_pi_detail", user_id=user_id), status_code=302
        )
    job = await enqueue_profile_job_if_absent(db, target, priority=INTERACTIVE_PRIORITY)
    await db.commit()
    if job is None:
        flash(request, "This account's access is denied; no profile is generated.", "error")
    else:
        flash(request, "Profile generation queued.", "success")
    return RedirectResponse(
        url=page_url(request, "workspace_pi_detail", user_id=user_id), status_code=302
    )


# --------------------------------------------------------------------------- #
# Corpus and profile review (spec 2026-10-05 §6.3 Review UIs, D21, D45, D47, D71).
# Every route: _STAFF, _require_pi, lookups scoped by user_id, the persona writer lock
# (bounded) before any row, the effective user recorded, a WARNING under impersonation.
# --------------------------------------------------------------------------- #


def _review_redirect(
    request: Request, user_id: uuid.UUID, error: str | None = None
) -> RedirectResponse:
    return RedirectResponse(
        url=page_url(
            request,
            "workspace_pi_detail",
            user_id=user_id,
            query={"error": error},
            fragment="review",
        ),
        status_code=302,
    )


def _review_busy(request: Request, user_id: uuid.UUID) -> RedirectResponse:
    flash(request, "This PI's profile is being updated — try again in a moment.", "error")
    return _review_redirect(request, user_id, "persona_busy")


async def _publish(
    request: Request,
    db: AsyncSession,
    target_id: uuid.UUID,
    current_user: User,
    *,
    mechanism: str,
    summary: str,
    action: str,
) -> None:
    """Record the revision (``reexport_persona``), commit, write the persona file."""
    note = _warn_if_impersonated(request, current_user, action, target_id)
    await reexport_persona(
        db,
        target_id,
        mechanism=mechanism,
        changed_by_user_id=current_user.id,
        change_summary=note or summary,
    )
    await db.commit()
    await write_persona_files(db, target_id)


def _log_actor(current_user: User, action: str, user_id: uuid.UUID) -> None:
    """Record who acted where no column does (Keep, Discard, Regenerate)."""
    logger.info("Staff user %s: %s on PI %s", current_user.id, action, user_id)


async def _no_rows() -> None:
    return None


@router.post("/pis/{user_id}/candidates/{candidate_id}/accept")
async def workspace_accept_candidate(
    user_id: uuid.UUID,
    candidate_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Store a candidate paper with provenance ``manual`` from its PubMed record (fetched
    before any lock; nothing is written when PubMed fails) and record a ``paper_review``
    revision. 404 for another PI's or an already decided candidate."""
    await _require_pi(db, user_id)
    pmid = await db.scalar(
        select(PublicationCandidate.pmid).where(
            PublicationCandidate.id == candidate_id,
            PublicationCandidate.user_id == user_id,
            PublicationCandidate.status == "pending",
        )
    )
    if pmid is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    try:
        records = await fetch_pubmed_records([pmid], strict=True)
    except Exception:
        logger.warning("PubMed fetch for candidate PMID %s failed", pmid, exc_info=True)
        return _review_redirect(request, user_id, "pubmed_unreachable")
    record = next((r for r in records if str(r.get("pmid")) == pmid), None)
    if record is None:
        return _review_redirect(request, user_id, "pubmed_not_found")
    actor_id = current_user.id
    try:
        cand = await _bounded_persona_locks(
            db,
            user_id,
            PERSONA_LOCK_TIMEOUT,
            lambda: profile_review.accept_candidate(
                db, user_id, candidate_id, record, actor_id=actor_id
            ),
        )
    except _PersonaBusy:
        return _review_busy(request, user_id)
    if cand is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    await _publish(
        request,
        db,
        user_id,
        current_user,
        mechanism="paper_review",
        summary=f"Paper accepted: PMID {pmid}",
        action="Candidate paper accept",
    )
    flash(request, "Paper added to the corpus.", "success")
    return _review_redirect(request, user_id)


@router.post("/pis/{user_id}/candidates/{candidate_id}/reject")
async def workspace_reject_candidate(
    user_id: uuid.UUID,
    candidate_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Reject a candidate paper; the row is kept so it is never offered again."""
    await _require_pi(db, user_id)
    actor_id = current_user.id
    try:
        cand = await _bounded_persona_locks(
            db,
            user_id,
            PERSONA_LOCK_TIMEOUT,
            lambda: profile_review.reject_candidate(db, user_id, candidate_id, actor_id=actor_id),
        )
    except _PersonaBusy:
        return _review_busy(request, user_id)
    if cand is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    _warn_if_impersonated(request, current_user, "Candidate paper reject", user_id)
    await db.commit()
    flash(request, "Paper rejected; it will not be offered again.", "success")
    return _review_redirect(request, user_id)


@router.post("/pis/{user_id}/publications/{publication_id}/keep")
async def workspace_keep_publication(
    user_id: uuid.UUID,
    publication_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Keep an unanchored paper (provenance ``manual``). It changes no rendered text, so no
    revision is recorded; anything but an unanchored, non-excluded row is a no-op."""
    await _require_pi(db, user_id)
    try:
        row = await _bounded_persona_locks(
            db,
            user_id,
            PERSONA_LOCK_TIMEOUT,
            lambda: profile_review.keep_publication(db, user_id, publication_id),
        )
    except _PersonaBusy:
        return _review_busy(request, user_id)
    if row is None:
        return _review_redirect(request, user_id)
    _warn_if_impersonated(request, current_user, "Publication keep", user_id)
    _log_actor(current_user, f"kept publication PMID {row.pmid}", user_id)
    await db.commit()
    flash(request, "Kept.", "success")
    return _review_redirect(request, user_id)


@router.post("/pis/{user_id}/publications/{publication_id}/exclude")
async def workspace_exclude_publication(
    user_id: uuid.UUID,
    publication_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Exclude a stored paper (kept, ignored by every reader: D45) and record a
    ``paper_review`` revision of the re-rendered persona."""
    await _require_pi(db, user_id)
    actor_id = current_user.id
    try:
        row = await _bounded_persona_locks(
            db,
            user_id,
            PERSONA_LOCK_TIMEOUT,
            lambda: profile_review.exclude_publication(
                db, user_id, publication_id, actor_id=actor_id
            ),
        )
    except _PersonaBusy:
        return _review_busy(request, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Publication not found")
    await _publish(
        request,
        db,
        user_id,
        current_user,
        mechanism="paper_review",
        summary=f"Paper excluded: PMID {row.pmid}",
        action="Publication exclude",
    )
    flash(request, "Paper excluded.", "success")
    return _review_redirect(request, user_id)


@router.post("/pis/{user_id}/publications/{publication_id}/restore")
async def workspace_restore_publication(
    user_id: uuid.UUID,
    publication_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Undo an Exclude (D71): the paper is read again everywhere; a ``paper_review``
    revision of the re-rendered persona records the acting user."""
    await _require_pi(db, user_id)
    try:
        row = await _bounded_persona_locks(
            db,
            user_id,
            PERSONA_LOCK_TIMEOUT,
            lambda: profile_review.restore_publication(db, user_id, publication_id),
        )
    except _PersonaBusy:
        return _review_busy(request, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Publication not found")
    await _publish(
        request,
        db,
        user_id,
        current_user,
        mechanism="paper_review",
        summary=f"Paper restored: PMID {row.pmid}",
        action="Publication restore",
    )
    flash(request, "Paper restored to the corpus.", "success")
    return _review_redirect(request, user_id)


@router.post("/pis/{user_id}/draft/accept")
async def workspace_accept_draft(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Accept the staged regeneration draft (D19): refused when it is stale or a generation
    is in flight; records a ``draft_accept`` revision and exports."""
    await _require_pi(db, user_id)
    try:
        code = await _bounded_persona_locks(
            db,
            user_id,
            PERSONA_LOCK_TIMEOUT,
            lambda: profile_review.accept_draft(db, user_id),
        )
    except _PersonaBusy:
        return _review_busy(request, user_id)
    if code is not None:
        await db.rollback()
        return _review_redirect(request, user_id, code)
    await _publish(
        request,
        db,
        user_id,
        current_user,
        mechanism="draft_accept",
        summary="Regenerated profile draft accepted",
        action="Draft accept",
    )
    flash(request, "Draft accepted.", "success")
    return _review_redirect(request, user_id)


@router.post("/pis/{user_id}/draft/discard")
async def workspace_discard_draft(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Discard the staged regeneration draft; the profile is unchanged."""
    await _require_pi(db, user_id)
    try:
        discarded = await _bounded_persona_locks(
            db,
            user_id,
            PERSONA_LOCK_TIMEOUT,
            lambda: profile_review.discard_draft(db, user_id),
        )
    except _PersonaBusy:
        return _review_busy(request, user_id)
    if discarded:
        _warn_if_impersonated(request, current_user, "Draft discard", user_id)
        _log_actor(current_user, "discarded the profile draft", user_id)
    await db.commit()
    flash(
        request,
        "Draft discarded." if discarded else "No draft to discard.",
        "success" if discarded else "error",
    )
    return _review_redirect(request, user_id)


@router.post("/pis/{user_id}/regenerate")
async def workspace_regenerate_profile(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Queue a profile generation for any profile, refused while one is in flight or within
    ``profile_review.REGENERATE_COOLDOWN`` of the last completed one. A profile edited since
    its last generation gets a draft to review instead of an overwrite (D19)."""
    target = await _require_pi(db, user_id)
    refusal = await profile_review.regenerate_refusal(db, user_id)
    if refusal is not None:
        flash(request, refusal, "error")
        return _review_redirect(request, user_id, "regenerate_refused")
    job = await enqueue_profile_job_if_absent(db, target, priority=INTERACTIVE_PRIORITY)
    _warn_if_impersonated(request, current_user, "Profile regenerate", user_id)
    _log_actor(current_user, "queued a profile regeneration", user_id)
    await db.commit()
    if job is None:
        flash(request, "This account's access is denied; no profile is generated.", "error")
    else:
        flash(
            request,
            "Profile generation queued. A profile edited since its last generation gets a "
            "draft to review.",
            "success",
        )
    return _review_redirect(request, user_id)


@router.post("/pis/{user_id}/persona/reexport")
async def workspace_reexport_persona(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Re-render the persona, record a ``reexport`` revision, commit and write the file
    (the repair for a failed post-commit write, ``agents.persona_export_failed_at``)."""
    await _require_pi(db, user_id)
    try:
        await _bounded_persona_locks(db, user_id, PERSONA_LOCK_TIMEOUT, _no_rows)
    except _PersonaBusy:
        return _review_busy(request, user_id)
    note = _warn_if_impersonated(request, current_user, "Persona re-export", user_id)
    rendered = await reexport_persona(
        db,
        user_id,
        mechanism="reexport",
        changed_by_user_id=current_user.id,
        change_summary=note or "Staff re-export",
    )
    await db.commit()
    if rendered is None:
        flash(request, "This PI has no agent or profile to export.", "error")
    elif await write_persona_files(db, user_id) is None:
        flash(request, "Re-export failed; see the ERROR log.", "error")
    else:
        flash(request, "Persona file re-exported.", "success")
    return _review_redirect(request, user_id)
