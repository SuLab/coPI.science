"""PI workflow helpers; preserve locks, provenance and partial-failure ordering."""

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TypeVar

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.role_capabilities import requires_linked_user
from src.database import get_db
from src.dependencies import (
    get_staff_user,
    impersonation_note,
)
from src.models import (
    USER_ROLE_PI,
    AgentRegistry,
    Job,
    PiCompany,
    User,
)
from src.services.company_discovery import (
    DISCOVERY_DONE_STEP,
)
from src.services.person_names import InvalidPersonName
from src.services.pi_companies import (
    evidence_for_display,
    format_funding,
    http_url,
)
from src.services.pi_onboarding import (
    OrcidNameRequired,
)
from src.services.profile_publish import (
    lock_persona_writer,
)
from src.web.urls import page_url

logger = logging.getLogger(__name__)


_DB = Depends(get_db)

_STAFF = Depends(get_staff_user)


def _create_pi_error_code(exc: ValueError) -> str:
    """Map service failures to canned codes — the raw exception text used to
    be interpolated into the redirect Location unescaped (it embeds the
    submitted ORCID string and upstream httpx internals; audit L1)."""
    if isinstance(exc, OrcidNameRequired):
        return "name_required"
    if isinstance(exc, InvalidPersonName):
        return "invalid_name"
    text = str(exc)
    if "Invalid ORCID" in text:
        return "invalid_orcid"
    if "already exists" in text:
        return "exists"
    if "Could not fetch" in text:
        return "fetch_failed"
    return "create_failed"


PERSONA_LOCK_TIMEOUT = "5s"

_LOCK_NOT_AVAILABLE = "55P03"

_T = TypeVar("_T")


class _PersonaBusy(Exception):
    """A bounded lock wait expired (SQLSTATE 55P03); the transaction was rolled back."""


async def _bounded_persona_locks(
    db: AsyncSession,
    user_id: uuid.UUID,
    timeout: str,
    lock_rows: Callable[[], Awaitable[_T]],
) -> _T:
    """``lock_persona_writer`` for ``user_id``, then ``lock_rows()``, each lock wait bounded
    by ``timeout`` (``SET LOCAL lock_timeout``, reset to the default afterwards).

    Raises ``_PersonaBusy`` after rolling back when a wait expires, and a 404 when the PI's
    account was deleted while this waited (deletion takes the same advisory lock before it
    deletes anything, so the re-check under the lock is final; without it an identity-row
    INSERT would fail on the foreign key)."""
    await db.execute(text(f"SET LOCAL lock_timeout = '{timeout}'"))
    try:
        await lock_persona_writer(db, user_id)
        if await db.scalar(select(User.id).where(User.id == user_id)) is None:
            raise HTTPException(status_code=404, detail="PI not found")
        result = await lock_rows()
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != _LOCK_NOT_AVAILABLE:
            raise
        await db.rollback()
        raise _PersonaBusy from exc
    await db.execute(text("SET LOCAL lock_timeout TO DEFAULT"))
    return result


def _warn_if_impersonated(
    request: Request, current_user: User, action: str, user_id: uuid.UUID
) -> str | None:
    """The impersonation note for the revision, after a WARNING naming the real session
    holder (the attribution columns name the worn account, operator decision 2026-09-10)."""
    note = impersonation_note(current_user)
    if note:
        logger.warning(
            "%s on PI %s recorded under impersonated user %s; real session holder is %s",
            action,
            user_id,
            current_user.id,
            request.session.get("user_id"),
        )
    return note


async def _require_pi(db: AsyncSession, user_id: uuid.UUID) -> User:
    """The PI account ``user_id``, or 404 — never a staff account, so a manager
    cannot act on (or probe) an admin's row by UUID (A-17)."""
    target = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if target is None or target.user_role != USER_ROLE_PI:
        raise HTTPException(status_code=404, detail="PI not found")
    return target


async def _pending_pi_agent(db: AsyncSession, user_id: uuid.UUID) -> AgentRegistry:
    """The pending ``pi_lab`` agent of a PI account, or 404.

    404 rather than a redirect for a non-PI target, matching
    ``manager_pi_detail``: a manager must not be able to probe an admin's row
    by guessing a UUID. Also 404 for an agent that is not ``pending`` — the
    two routes below are the pending-agent onboarding path, and mute/unmute
    (design D4) already own the active/inactive transitions.

    ``role == "pi_lab"`` is required for a third, sharper reason:
    ``activation_blockers`` short-circuits to ``[]`` for any other role (the
    hub and the specialists have no PI profile by design), so a `pi` user
    hand-linked on /admin/agents to a hub or specialist row would sail past
    the gate here with no profile check at all. 404 instead.
    """
    await _require_pi(db, user_id)
    agent = (
        await db.execute(select(AgentRegistry).where(AgentRegistry.user_id == user_id))
    ).scalar_one_or_none()
    if agent is None or agent.status != "pending" or not requires_linked_user(agent.role):
        raise HTTPException(status_code=404, detail="No pending agent for this PI")
    return agent


def _company_view(row: PiCompany) -> dict:
    """One Companies-card entry: the row, its funding wording, its two links (http(s) or
    None) and, for a suggestion, its evidence as the card shows it."""
    return {
        "row": row,
        "funding": format_funding(row.funding_usd, row.funding_as_of, row.funding_source_url),
        "source_url": http_url(row.source_url),
        "funding_source_url": http_url(row.funding_source_url),
        "evidence": evidence_for_display(row.evidence),
    }


def _discovery_view(job: Job | None) -> dict | None:
    """What the Companies card says about discovery (spec 2026-10-02 §7.2), from the PI's
    latest company_discovery job: whether it is queued or running (the Find companies
    button is then disabled), when it last ran (``completed_at``, else ``enqueued_at``),
    its outcome (the detail of its last DISCOVERY_DONE_STEP progress entry), for a
    failed run, the start of its ``last_error`` and, for a job that deferred itself at the
    COI budget ceiling (``last_error`` "deferred until …", src/worker/main.py
    ``_mark_deferred``), until when it waits. None when discovery was never queued."""
    if job is None:
        return None
    progress = (job.payload or {}).get("progress") or []
    outcome = next(
        (
            entry.get("detail")
            for entry in reversed(progress)
            if isinstance(entry, dict) and entry.get("step") == DISCOVERY_DONE_STEP
        ),
        None,
    )
    return {
        "active": job.status in ("pending", "processing"),
        "status": job.status,
        "at": job.completed_at or job.enqueued_at,
        "outcome": outcome or None,
        "error": (job.last_error or "")[:200] or None,
        "deferred_until": job.not_before
        if (job.status == "pending" and (job.last_error or "").startswith("deferred until"))
        else None,
    }


def _companies_redirect(request: Request, user_id: uuid.UUID) -> RedirectResponse:
    """Back to the PI page's Companies card, as the full literal path."""
    return RedirectResponse(
        url=page_url(request, "workspace_pi_detail", user_id=user_id, fragment="companies"),
        status_code=302,
    )
