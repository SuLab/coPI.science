"""Staff page: manager and reviewer accounts with no lab bot (plan
docs/plans/2026-10-13-staff-accounts-without-a-bot-plan.md §5.2).

The rules live in ``src.services.staff_accounts``; these handlers translate its refusals
into a flash and a redirect. Each handler declares ``_STAFF`` itself, with no router-level
dependency: the permissions matrix expects exactly one ``get_staff_user`` per POST.
"""

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_staff_user, refuse_impersonation
from src.models import User
from src.services.pi_onboarding import validate_orcid
from src.services.staff_accounts import (
    REFUSAL_MESSAGES,
    STAFF_ACCOUNT_ROLES,
    STAFF_REFILL_SESSION_KEY,
    StaffAccountRefused,
    creatable_roles,
    create_or_convert,
    delete_reviewer,
    list_staff_accounts,
    preview,
    recent_staff_changes,
    restore_reviewer,
    revoke_reviewer,
)
from src.web.flash import flash
from src.web.page_context import workspace_context
from src.web.templating import make_templates
from src.web.urls import page_url

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()
_DB = Depends(get_db)
_STAFF = Depends(get_staff_user)
# Every add-form field defaults to "": a malformed post is refused with a code, never a 422.
_ORCID_FORM = Form("")
_ROLE_FORM = Form("")
_NAME_FORM = Form("")
_CONFIRM_FORM = Form("")
_EXPECTATION_FORM = Form("")

#: The longest name the refill keeps (the person-name allowlist's limit).
_REFILL_NAME_CHARS = 100


def _refill(orcid: str, user_role: str, name: str) -> dict[str, Any]:
    """The add form's values, kept only when valid: they are re-rendered into the form."""
    try:
        clean_orcid = validate_orcid(orcid)
    except ValueError:
        clean_orcid = ""
    return {
        "orcid": clean_orcid,
        "user_role": user_role if user_role in STAFF_ACCOUNT_ROLES else "",
        "name": name.strip()[:_REFILL_NAME_CHARS],
    }


async def _render(
    request: Request,
    db: AsyncSession,
    current_user: User,
    *,
    staff_preview: dict | None = None,
    staff_preview_changed: bool = False,
    staff_refill: dict | None = None,
):
    """The Staff page, for the GET and for the add form's 200 responses (preview and
    ``changed``)."""
    return templates.TemplateResponse(
        request,
        "workspace/staff.html",
        workspace_context(
            request,
            current_user,
            section="staff",
            staff_rows=await list_staff_accounts(db, viewer=current_user),
            staff_roles=list(creatable_roles(current_user)),
            staff_preview=staff_preview,
            staff_preview_changed=staff_preview_changed,
            staff_refill=staff_refill,
            staff_changes=await recent_staff_changes(db),
        ),
    )


def _back_to_staff(request: Request) -> RedirectResponse:
    """Every outcome but a 200 render lands here. The URL carries nothing but the path:
    refusal details travel in the signed session (flash and refill), so the permissions
    matrix sees the same ``Location`` for every target and nobody can link a victim to a
    page saying whatever they like."""
    return RedirectResponse(url=page_url(request, "workspace_staff"), status_code=302)


async def _refuse_add(
    request: Request,
    db: AsyncSession,
    code: str,
    refill: dict,
    *,
    user_id: uuid.UUID | None = None,
) -> RedirectResponse:
    await db.rollback()
    message = REFUSAL_MESSAGES[code]
    if code == "use_account_page" and user_id is not None:
        # Only an admin is given this code (a manager gets ``ask_admin``), so the admin
        # path is safe to show; the refill carries it as a link for the template too.
        message = f"{message} (/admin/users/{user_id})"
        # The id, not a URL: the template builds the link, so the reachability gate can
        # check it.
        refill = {**refill, "account_user_id": str(user_id)}
    flash(request, message, "error")
    request.session[STAFF_REFILL_SESSION_KEY] = refill
    return _back_to_staff(request)


@router.get("/staff", response_class=HTMLResponse, name="workspace_staff")
async def workspace_staff(
    request: Request, db: AsyncSession = _DB, current_user: User = _STAFF
):
    """Every manager, reviewer and admin, the add form and recent changes. A refill left
    by a refused add is shown once."""
    refill = request.session.pop(STAFF_REFILL_SESSION_KEY, None)
    return await _render(
        request, db, current_user, staff_refill=refill if isinstance(refill, dict) else None
    )


@router.post("/staff", name="workspace_create_staff_account")
async def workspace_create_staff_account(
    request: Request,
    orcid: str = _ORCID_FORM,
    user_role: str = _ROLE_FORM,
    name: str = _NAME_FORM,
    confirm: str = _CONFIRM_FORM,
    expectation: str = _EXPECTATION_FORM,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Add a manager or reviewer in two steps (O2): without ``confirm=1`` show who the
    iD belongs to and what will happen (200); with it, act. A confirm whose facts changed
    since the preview re-renders the fresh preview (200) and writes nothing."""
    refuse_impersonation(current_user, "Staff account changes are disabled while impersonating.")
    refill = _refill(orcid, user_role, name)
    entered_name = name.strip() or None
    if confirm != "1":
        try:
            shown = await preview(
                db, actor=current_user, orcid=orcid, role=user_role, name=entered_name
            )
        except StaffAccountRefused as exc:
            return await _refuse_add(request, db, exc.code, refill, user_id=exc.user_id)
        except Exception:
            logger.exception("Staff account preview failed for ORCID %r", orcid.strip()[:40])
            return await _refuse_add(request, db, "create_failed", refill)
        return await _render(
            request, db, current_user, staff_preview=shown.as_view(), staff_refill=refill
        )
    try:
        user, converted = await create_or_convert(
            db,
            actor=current_user,
            orcid=orcid,
            role=user_role,
            name=entered_name,
            expectation=expectation,
        )
        created_name, created_role = user.name, user.user_role
        await db.commit()
    except StaffAccountRefused as exc:
        if exc.code != "changed" or exc.preview is None:
            return await _refuse_add(request, db, exc.code, refill, user_id=exc.user_id)
        fresh = exc.preview.as_view()
        await db.rollback()
        # The rollback expired the session's objects; the page reads the viewer.
        await db.refresh(current_user)
        return await _render(
            request, db, current_user,
            staff_preview=fresh, staff_preview_changed=True, staff_refill=refill,
        )
    except Exception:
        logger.exception("Staff account add failed for ORCID %r", orcid.strip()[:40])
        return await _refuse_add(request, db, "create_failed", refill)
    if converted:
        flash(request, f"{created_name}'s account is now a {created_role} account.", "success")
    else:
        flash(request, f"Created a {created_role} account for {created_name}.", "success")
    return _back_to_staff(request)


async def _reviewer_action(
    request: Request,
    db: AsyncSession,
    current_user: User,
    user_id: uuid.UUID,
    *,
    action,
    success: str,
    commits: bool = False,
) -> RedirectResponse:
    """Run one reviewer action and redirect. Every refusal, a missing or non-reviewer
    target included, is a flash and a 302 rather than a 404: the permissions matrix
    expects the same 302 for a PI target as for a reviewer one, and the page is the only
    place these buttons live."""
    try:
        await action(db, actor=current_user, user_id=user_id)
        if not commits:
            await db.commit()
    except StaffAccountRefused as exc:
        await db.rollback()
        flash(request, REFUSAL_MESSAGES[exc.code], "error")
        return _back_to_staff(request)
    except Exception:
        await db.rollback()
        logger.exception("Staff page reviewer action failed for user %s", user_id)
        flash(request, REFUSAL_MESSAGES["create_failed"], "error")
        return _back_to_staff(request)
    flash(request, success, "success")
    return _back_to_staff(request)


@router.post("/staff/{user_id}/revoke", name="workspace_revoke_reviewer")
async def workspace_revoke_reviewer(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Deny a reviewer's access and sign them out everywhere (D5)."""
    refuse_impersonation(current_user, "Staff account changes are disabled while impersonating.")
    return await _reviewer_action(
        request, db, current_user, user_id,
        action=revoke_reviewer,
        success="Reviewer access revoked; they are signed out everywhere.",
    )


@router.post("/staff/{user_id}/restore", name="workspace_restore_reviewer")
async def workspace_restore_reviewer(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Give a denied reviewer access again (D6)."""
    refuse_impersonation(current_user, "Staff account changes are disabled while impersonating.")
    return await _reviewer_action(
        request, db, current_user, user_id,
        action=restore_reviewer,
        success="Reviewer access restored.",
    )


@router.post("/staff/{user_id}/delete", name="workspace_delete_reviewer")
async def workspace_delete_reviewer(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """Delete a reviewer account (D5); ``delete_reviewer`` commits."""
    refuse_impersonation(current_user, "Staff account changes are disabled while impersonating.")
    return await _reviewer_action(
        request, db, current_user, user_id,
        action=delete_reviewer,
        success="Reviewer account deleted.",
        commits=True,
    )
