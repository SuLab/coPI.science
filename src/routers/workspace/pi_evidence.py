"""Canonical PI grants, industry evidence, and company write registrations."""

import re
import uuid
from datetime import UTC, datetime
from typing import TypeVar

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import PiGrant, PiGrantIdentity, PiIndustryEvidence, PiOrcidFunding, User
from src.models.job import INTERACTIVE_PRIORITY
from src.services.company_discovery import enqueue_company_discovery
from src.services.industry_evidence import rescore_user
from src.services.job_queue import request_job
from src.services.nih_reporter import profile_id_exists
from src.services.pi_companies import (
    CompanyNotFoundError,
    CompanyValidationError,
    add_company,
    confirm_company,
    delete_company,
    parse_funding_as_of,
    parse_funding_usd,
    reject_company,
)
from src.services.profile_publish import reexport_persona, write_persona_files
from src.web.flash import flash
from src.web.urls import page_url

from ._pi_common import (
    _DB,
    _STAFF,
    _bounded_persona_locks,
    _companies_redirect,
    _PersonaBusy,
    _require_pi,
    _warn_if_impersonated,
    logger,
)

router = APIRouter()
#: synthesis included); past this the veto says "try again".
ORCID_VETO_LOCK_TIMEOUT = "5s"
#: ``SET LOCAL lock_timeout`` for the RePORTER grant veto, pin, none and unpin routes: the
#: same persona-lock wait as the ORCID veto; past this the route says "try again".
PERSONA_LOCK_TIMEOUT = "5s"
#: ``SET LOCAL lock_timeout`` for the industry-evidence veto (spec 2026-10-05 §6.2): an
#: industry_evidence job holds the PI's evidence rows from its upsert until it commits
#: (its rescore follows at once); past this the veto says "try again".
INDUSTRY_VETO_LOCK_TIMEOUT = "5s"
#: ``SET LOCAL lock_timeout`` for Find companies: a profile generation's step 10 holds a
#: pending discovery job's row (`job_queue.request_job`) until the generation commits.
DISCOVERY_ENQUEUE_LOCK_TIMEOUT = "5s"
#: Postgres SQLSTATE lock_not_available.
_LOCK_NOT_AVAILABLE = "55P03"


_T = TypeVar("_T")


def _persona_busy_redirect(request: Request, user_id: uuid.UUID) -> RedirectResponse:
    flash(request, "This PI's profile is being updated — try again in a moment.", "error")
    return _grants_redirect(request, user_id, "persona_busy")


def _grants_redirect(
    request: Request, user_id: uuid.UUID, error: str | None = None
) -> RedirectResponse:
    return RedirectResponse(
        url=page_url(
            request,
            "workspace_pi_detail",
            user_id=user_id,
            query={"error": error},
            fragment="grants",
        ),
        status_code=302,
    )


#: A RePORTER profile id as the pin form accepts it: ASCII digits only (``str.isdigit``
#: also admits "²" and digit strings ``int()`` refuses), within Postgres ``integer``.
_PROFILE_ID_RE = re.compile(r"[0-9]{1,10}")
_PROFILE_ID_MAX = 2_147_483_647


def _parse_profile_id(value: object) -> int | None:
    """``value`` as a RePORTER profile id (1..2^31-1), or None when it is not one."""
    if not isinstance(value, str) or not _PROFILE_ID_RE.fullmatch(value):
        return None
    parsed = int(value)
    return parsed if 1 <= parsed <= _PROFILE_ID_MAX else None


async def _locked_identity_row(
    db: AsyncSession, user_id: uuid.UUID, *, create: bool
) -> PiGrantIdentity | None:
    """The PI's ``pi_grant_identity`` row, locked FOR UPDATE. With ``create`` an absent row
    is inserted first (``ON CONFLICT DO NOTHING``, so a concurrent insert is not an
    IntegrityError); without it an absent row returns None. The caller has already taken
    ``lock_persona_writer`` and re-checked that the user exists (``_bounded_persona_locks``),
    so the insert cannot fail on the foreign key."""
    if create:
        await db.execute(
            pg_insert(PiGrantIdentity)
            .values(user_id=user_id)
            .on_conflict_do_nothing(index_elements=[PiGrantIdentity.user_id])
        )
    return (
        await db.execute(
            select(PiGrantIdentity).where(PiGrantIdentity.user_id == user_id).with_for_update()
        )
    ).scalar_one_or_none()


async def _identity_row(db: AsyncSession, user_id: uuid.UUID) -> PiGrantIdentity:
    """The PI's ``pi_grant_identity`` row locked FOR UPDATE, inserted when absent."""
    row = await _locked_identity_row(db, user_id, create=True)
    if row is None:  # defence in depth: the caller's lock rules out a concurrent deletion
        raise HTTPException(status_code=404, detail="PI not found")
    return row


async def _after_identity_change(
    request: Request,
    db: AsyncSession,
    target: User,
    current_user: User,
    summary: str,
    action: str,
) -> None:
    """Record a ``grant_pin`` revision, request ``enrich_grants`` (a rerun flag when one is
    processing), commit, then write the persona file."""
    note = _warn_if_impersonated(request, current_user, action, target.id)
    await reexport_persona(
        db,
        target.id,
        mechanism="grant_pin",
        changed_by_user_id=current_user.id,
        change_summary=note or summary,
    )
    await request_job(
        db,
        type="enrich_grants",
        user_id=target.id,
        payload={"user_id": str(target.id), "orcid": target.orcid},
        priority=INTERACTIVE_PRIORITY,
    )
    await db.commit()
    await write_persona_files(db, target.id)


@router.post("/pis/{user_id}/grants/{grant_id}/veto")
async def workspace_veto_grant(
    user_id: uuid.UUID,
    grant_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Mark a RePORTER award as 'not this PI'. Persisted; re-runs keep it (vetoed rows are
    never deleted). Records who vetoed and a `grant_veto` revision of the re-rendered
    persona (U-10); profile_version is untouched, grants are not profile fields. A second
    veto is a no-op (D-19). 404 for a non-PI account or another PI's grant (A-17). Waits at
    most ``PERSONA_LOCK_TIMEOUT`` for the PI's persona lock or the row, then says "try
    again"."""
    await _require_pi(db, user_id)

    async def lock_grant() -> PiGrant | None:
        return (
            await db.execute(
                select(PiGrant)
                .where(PiGrant.id == grant_id, PiGrant.user_id == user_id)
                .with_for_update()
            )
        ).scalar_one_or_none()

    try:
        # The persona writer lock before the child-row lock (spec §4.3 order).
        grant = await _bounded_persona_locks(db, user_id, PERSONA_LOCK_TIMEOUT, lock_grant)
    except _PersonaBusy:
        return _persona_busy_redirect(request, user_id)
    if grant is None:
        raise HTTPException(status_code=404, detail="Grant not found")
    if grant.vetoed_at is not None:
        return _grants_redirect(request, user_id)
    grant.vetoed_at = datetime.now(UTC)
    grant.vetoed_by_user_id = current_user.id
    note = _warn_if_impersonated(request, current_user, "RePORTER grant veto", user_id)
    await reexport_persona(
        db,
        user_id,
        mechanism="grant_veto",
        changed_by_user_id=current_user.id,
        change_summary=note or f"RePORTER grant vetoed: {grant.core_project_num}",
    )
    await db.commit()
    await write_persona_files(db, user_id)
    return _grants_redirect(request, user_id)


@router.post("/pis/{user_id}/grant-identity/pin")
async def workspace_pin_grant_identity(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """ "This is the PI": pin one or more RePORTER profile ids (a split record has several).
    Ids come from the stored candidates (checkboxes) or one typed id, which a single
    pi_profile_ids search must confirm. enrich_grants then fetches those ids' awards."""
    target = await _require_pi(db, user_id)
    form = await request.form()
    typed = str(form.get("profile_id_text") or "").strip()
    typed_id: int | None = None
    if typed:
        # The RePORTER call happens before the locks below, never while holding them.
        typed_id = _parse_profile_id(typed)
        if typed_id is None:
            return _grants_redirect(request, user_id, "invalid_reporter_id")
        try:
            exists = await profile_id_exists(typed_id)
        except (httpx.HTTPError, ValueError, AttributeError, TypeError, KeyError):
            # Unreachable, an error status, or a body that is not JSON of the expected shape.
            return _grants_redirect(request, user_id, "reporter_unreachable")
        if not exists:
            return _grants_redirect(request, user_id, "invalid_reporter_id")
    try:
        identity = await _bounded_persona_locks(
            db, user_id, PERSONA_LOCK_TIMEOUT, lambda: _identity_row(db, user_id)
        )
    except _PersonaBusy:
        return _persona_busy_redirect(request, user_id)
    offered = {
        pid
        for c in (identity.candidates or [])
        if isinstance(c, dict)
        if (pid := _parse_profile_id(str(c.get("id", "")))) is not None
    }
    ids = {
        pid
        for v in form.getlist("profile_ids")
        if (pid := _parse_profile_id(v)) is not None and pid in offered
    }
    if typed_id is not None:
        ids.add(typed_id)
    if not ids:
        return _grants_redirect(request, user_id, "no_profile_selected")
    identity.pinned_profile_ids = sorted(ids)
    identity.none_confirmed = False
    identity.pinned_by_user_id = current_user.id
    identity.pinned_at = datetime.now(UTC)
    identity.status = "pinned"
    await _after_identity_change(
        request,
        db,
        target,
        current_user,
        f"RePORTER profile pinned: {sorted(ids)}",
        "Grant identity pin",
    )
    flash(request, "RePORTER profile pinned; grants are being refreshed.", "success")
    return _grants_redirect(request, user_id)


@router.post("/pis/{user_id}/grant-identity/none")
async def workspace_confirm_no_reporter_profile(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """ "PI has no RePORTER profile": no RePORTER grants for this PI until Unpin."""
    target = await _require_pi(db, user_id)
    try:
        identity = await _bounded_persona_locks(
            db, user_id, PERSONA_LOCK_TIMEOUT, lambda: _identity_row(db, user_id)
        )
    except _PersonaBusy:
        return _persona_busy_redirect(request, user_id)
    identity.pinned_profile_ids = None
    identity.none_confirmed = True
    identity.pinned_by_user_id = current_user.id
    identity.pinned_at = datetime.now(UTC)
    identity.status = "none_confirmed"
    await _after_identity_change(
        request,
        db,
        target,
        current_user,
        "Marked: PI has no RePORTER profile",
        "Grant identity none-confirmed",
    )
    flash(request, "Marked: this PI has no RePORTER profile.", "success")
    return _grants_redirect(request, user_id)


@router.post("/pis/{user_id}/grant-identity/unpin")
async def workspace_unpin_grant_identity(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Remove a pin or a "no RePORTER profile" mark; enrich_grants re-runs the identity rule.
    No identity row means nothing to unpin, and none is created."""
    target = await _require_pi(db, user_id)
    try:
        identity = await _bounded_persona_locks(
            db,
            user_id,
            PERSONA_LOCK_TIMEOUT,
            lambda: _locked_identity_row(db, user_id, create=False),
        )
    except _PersonaBusy:
        return _persona_busy_redirect(request, user_id)
    if identity is None or (not identity.pinned_profile_ids and not identity.none_confirmed):
        return _grants_redirect(request, user_id)
    identity.pinned_profile_ids = None
    identity.none_confirmed = False
    identity.pinned_by_user_id = None
    identity.pinned_at = None
    identity.status = None
    await _after_identity_change(
        request,
        db,
        target,
        current_user,
        "RePORTER pin removed",
        "Grant identity unpin",
    )
    flash(request, "Pin removed; the RePORTER identity is being re-evaluated.", "success")
    return _grants_redirect(request, user_id)


@router.post("/pis/{user_id}/orcid-fundings/{funding_id}/veto")
async def workspace_veto_orcid_funding(
    user_id: uuid.UUID,
    funding_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """'Not this PI's' on one ORCID funding (D43); persisted across refreshes. Waits at
    most ``ORCID_VETO_LOCK_TIMEOUT`` for a refresh holding the PI's persona lock or the
    row, then says "try again"."""
    await _require_pi(db, user_id)

    async def lock_funding() -> PiOrcidFunding | None:
        return (
            await db.execute(
                select(PiOrcidFunding)
                .where(PiOrcidFunding.id == funding_id, PiOrcidFunding.user_id == user_id)
                .with_for_update()
            )
        ).scalar_one_or_none()

    try:
        # The persona writer lock before the child-row lock (spec §4.3 order).
        row = await _bounded_persona_locks(db, user_id, ORCID_VETO_LOCK_TIMEOUT, lock_funding)
    except _PersonaBusy:
        flash(request, "ORCID fundings are being refreshed — try again in a moment.", "error")
        return _grants_redirect(request, user_id, "orcid_busy")
    if row is None:
        raise HTTPException(status_code=404, detail="Funding not found")
    if row.vetoed_at is not None:
        return _grants_redirect(request, user_id)
    row.vetoed_at = datetime.now(UTC)
    row.vetoed_by_user_id = current_user.id
    note = _warn_if_impersonated(request, current_user, "ORCID funding veto", user_id)
    await reexport_persona(
        db,
        user_id,
        mechanism="orcid_veto",
        changed_by_user_id=current_user.id,
        change_summary=note or f"ORCID funding vetoed: {row.title[:120]}",
    )
    await db.commit()
    await write_persona_files(db, user_id)
    return _grants_redirect(request, user_id)


@router.post("/pis/{user_id}/industry/{evidence_id}/veto")
async def workspace_veto_industry_evidence(
    user_id: uuid.UUID,
    evidence_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """'Not this PI / not industry' veto on one evidence row; persisted and
    rescored immediately. Idempotent: a replayed POST on an already-vetoed
    row is a no-op redirect, so a double-click never rescores twice. Waits at most
    INDUSTRY_VETO_LOCK_TIMEOUT for the row, then says "try again" and changes nothing."""
    await _require_pi(db, user_id)
    await db.execute(text(f"SET LOCAL lock_timeout = '{INDUSTRY_VETO_LOCK_TIMEOUT}'"))
    try:
        row = (
            await db.execute(
                select(PiIndustryEvidence)
                .where(PiIndustryEvidence.id == evidence_id, PiIndustryEvidence.user_id == user_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Evidence not found")
        if row.vetoed_at is None:
            row.vetoed_at = datetime.now(UTC)
            # `current_user` is the EFFECTIVE user (`_STAFF` = `get_staff_user`),
            # which is the impersonated manager while an admin is impersonating
            # one — so the attribution column, per branch convention, still
            # names the worn account. The real actor is recorded only in this
            # log line, keyed off the session itself (unaffected by the
            # impersonation cookie) rather than off `current_user`.
            row.vetoed_by_user_id = current_user.id
            if getattr(current_user, "_is_impersonated", False):
                logger.warning(
                    "Industry-evidence veto on %s recorded under impersonated "
                    "user %s; real session holder is %s",
                    evidence_id,
                    current_user.id,
                    request.session.get("user_id"),
                )
            # No tenure_start passed: rescore_user re-reads it itself when omitted.
            await rescore_user(db, user_id)
            await db.commit()
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != _LOCK_NOT_AVAILABLE:
            raise
        await db.rollback()
        flash(
            request,
            "This PI's industry evidence is being refreshed — try again in a moment.",
            "error",
        )
        return RedirectResponse(
            url=page_url(
                request,
                "workspace_pi_detail",
                user_id=user_id,
                query={"error": "industry_busy"},
                fragment="industry",
            ),
            status_code=302,
        )
    return RedirectResponse(
        url=page_url(request, "workspace_pi_detail", user_id=user_id, fragment="industry"),
        status_code=302,
    )


@router.post("/pis/{user_id}/companies")
async def workspace_add_company(
    user_id: uuid.UUID,
    request: Request,
    company_name: str = Form(""),
    pi_role: str = Form(""),
    funding_usd: str = Form(""),
    funding_as_of: str = Form(""),
    source_url: str = Form(""),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Add a company by hand (spec 2026-10-02 §7.2). A manual entry is confirmed at once
    (O9) and reaches the hub's file straight away. ``created_by_user_id`` is the effective
    user, so an admin impersonating a manager records the manager, as every manager write
    does. A refused entry flashes its reason and changes nothing."""
    await _require_pi(db, user_id)
    try:
        row = await add_company(
            db,
            user_id=user_id,
            company_name=company_name,
            pi_role=pi_role,
            funding_usd=parse_funding_usd(funding_usd),
            funding_as_of=parse_funding_as_of(funding_as_of),
            source_url=source_url,
            created_by_user_id=current_user.id,
        )
    except CompanyValidationError as exc:
        flash(request, f"Company not added: {exc}", "error")
        return _companies_redirect(request, user_id)
    flash(request, f"Added {row.company_name}.", "success")
    return _companies_redirect(request, user_id)


@router.post("/pis/{user_id}/companies/discover")
async def workspace_discover_companies(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Find companies: queue company discovery for this PI at interactive priority (spec
    §7.5). While one is pending or processing a press inserts nothing (the per-user
    unique index), and a pending bulk job is raised to interactive instead. Discovery
    reads the PI's own papers and Wikidata by ORCID iD, so a PI without one is told so
    and nothing is queued. Waits at most DISCOVERY_ENQUEUE_LOCK_TIMEOUT behind a
    generation's step 10."""
    target = await _require_pi(db, user_id)
    if not (target.orcid or "").strip():
        flash(request, "Company discovery needs the PI's ORCID iD; none is recorded.", "error")
        return _companies_redirect(request, user_id)
    await db.execute(text(f"SET LOCAL lock_timeout = '{DISCOVERY_ENQUEUE_LOCK_TIMEOUT}'"))
    try:
        job_id = await enqueue_company_discovery(db, user_id, priority=INTERACTIVE_PRIORITY)
        await db.commit()
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != _LOCK_NOT_AVAILABLE:
            raise
        await db.rollback()
        flash(
            request,
            "A profile generation is queueing discovery for this PI right now — "
            "try again in a moment.",
            "error",
        )
        return _companies_redirect(request, user_id)
    if job_id is None:
        flash(request, "Company discovery is already queued or running for this PI.", "info")
    else:
        flash(
            request,
            "Company discovery queued. Its suggestions appear here when it finishes.",
            "success",
        )
    return _companies_redirect(request, user_id)


@router.post("/pis/{user_id}/companies/{company_id}/delete")
async def workspace_delete_company(
    user_id: uuid.UUID,
    company_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Remove a confirmed entry: it is recorded as rejected (spec 2026-10-05 §6.2, D37), so
    it leaves the hub's file at once and discovery never offers it again. The form asks
    first (``data-confirm``). A suggestion is rejected, never deleted."""
    await _require_pi(db, user_id)
    # The reviewer column names the worn account; the real session holder goes to the log.
    _warn_if_impersonated(request, current_user, "Company delete", user_id)
    try:
        row = await delete_company(
            db, user_id=user_id, company_id=company_id, reviewer_id=current_user.id
        )
    except CompanyNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Company not found") from exc
    except CompanyValidationError as exc:
        flash(request, str(exc), "error")
        return _companies_redirect(request, user_id)
    flash(request, f"Removed {row.company_name}; discovery will not suggest it again.", "success")
    return _companies_redirect(request, user_id)


@router.post("/pis/{user_id}/companies/{company_id}/confirm")
async def workspace_confirm_company(
    user_id: uuid.UUID,
    company_id: uuid.UUID,
    request: Request,
    pi_role: str = Form(""),
    funding_usd: str = Form(""),
    funding_as_of: str = Form(""),
    clear_funding: str = Form(""),
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Confirm a discovered suggestion, correcting its role and funding first where the
    form says so; a blank field keeps the stored value. The "Clear funding" checkbox
    (any non-blank ``clear_funding``) drops the figure, its date and its source instead,
    and the funding fields are then not parsed. Recorded as reviewed by the effective
    user."""
    await _require_pi(db, user_id)
    clearing = bool(clear_funding.strip())
    try:
        row = await confirm_company(
            db,
            user_id=user_id,
            company_id=company_id,
            reviewer_id=current_user.id,
            pi_role=pi_role.strip() or None,
            funding_usd=None if clearing else parse_funding_usd(funding_usd),
            funding_as_of=None if clearing else parse_funding_as_of(funding_as_of),
            clear_funding=clearing,
        )
    except CompanyNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Company not found") from exc
    except CompanyValidationError as exc:
        flash(request, f"Not confirmed: {exc}", "error")
        return _companies_redirect(request, user_id)
    flash(request, f"Confirmed {row.company_name}.", "success")
    return _companies_redirect(request, user_id)


@router.post("/pis/{user_id}/companies/{company_id}/reject")
async def workspace_reject_company(
    user_id: uuid.UUID,
    company_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Reject a discovered suggestion. The row stays, unlisted, so discovery never offers
    the name again (spec §7.5)."""
    await _require_pi(db, user_id)
    try:
        row = await reject_company(
            db,
            user_id=user_id,
            company_id=company_id,
            reviewer_id=current_user.id,
        )
    except CompanyNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Company not found") from exc
    except CompanyValidationError as exc:
        flash(request, str(exc), "error")
        return _companies_redirect(request, user_id)
    flash(request, f"Rejected {row.company_name}; discovery will not suggest it again.", "success")
    return _companies_redirect(request, user_id)
