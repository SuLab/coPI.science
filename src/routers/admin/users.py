"""Admin user list, detail, deletion and role routes."""

import logging
import uuid

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_admin_user, refuse_impersonation
from src.models import USER_ROLE_ADMIN, VALID_USER_ROLES, User
from src.routers.admin._common import _ADMIN, _DB, _template_context, router, templates
from src.services.admin_invariant import LastAdminError, ensure_admin_remains
from src.services.directory import list_pi_directory, load_user_detail
from src.services.email_verification import mark_email_verified
from src.services.session_epoch import bump_session_epoch
from src.services.user_deletion import delete_user_account

logger = logging.getLogger("src.routers.admin")


@router.get("", response_class=HTMLResponse)
@router.get("/users", response_class=HTMLResponse)
async def admin_users(
    request: Request,
    status_filter: str | None = None,
    institution_filter: str | None = None,
    claimed_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Admin users overview."""
    user_data = await list_pi_directory(
        db,
        status_filter=status_filter,
        institution_filter=institution_filter,
        claimed_filter=claimed_filter,
    )

    return templates.TemplateResponse(
        request,
        "admin/users.html",
        _template_context(
            request,
            current_user,
            active_admin="users",
            user_data=user_data,
            status_filter=status_filter,
            institution_filter=institution_filter,
            claimed_filter=claimed_filter,
        ),
    )



@router.get("/users/{user_id}", response_class=HTMLResponse)
async def admin_user_detail(
    user_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Admin user detail page."""
    detail = await load_user_detail(db, user_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="User not found")

    return templates.TemplateResponse(
        request,
        "admin/user_detail.html",
        _template_context(
            request,
            current_user,
            active_admin="users",
            target_user=detail["user"],
            profile=detail["profile"],
            publications=detail["publications"],
            pub_scope=detail["pub_scope"],
            jobs=detail["jobs"],
            valid_user_roles=VALID_USER_ROLES,
        ),
    )



@router.post("/users/{user_id}/delete")
async def admin_delete_user(
    user_id: uuid.UUID,
    request: Request,
    remove_from_allowlist: str = Form(""),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Delete a user account (admin only) — through the full teardown."""
    # Same guard as POST /profile/delete-account (deletion audit F8/D6):
    # under impersonation `current_user` is the impersonated admin, so the
    # self-delete check below compares against the WRONG identity and the
    # log line would attribute the deletion to someone who never acted.
    # Drop impersonation first; then delete.
    refuse_impersonation(current_user, "Account deletion is disabled while impersonating.")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    name = user.name
    report = await delete_user_account(
        db, user, remove_from_allowlist=bool(remove_from_allowlist)
    )
    logger.info(
        "Admin %s deleted user %s (%s): %s",
        current_user.name, name, user_id, report.summary(),
    )
    return RedirectResponse(url="/admin/users", status_code=302)



@router.post("/users/{user_id}/role")
async def admin_set_user_role(
    user_id: uuid.UUID,
    request: Request,
    user_role: str = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_admin_user),
):
    """Set a user's account type (admin only).

    Named for users, not agents: POST /agents/{agent_id}/role already exists
    and sets a BOT role (pi_lab / scout_hub), which is a different thing.
    """
    # Under impersonation `current_user` is the impersonated user, so the
    # own-role guard below would compare against the wrong identity.
    refuse_impersonation(current_user, "Role changes are disabled while impersonating.")
    if user_role not in VALID_USER_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role: {user_role}")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Mirrors the self-delete guard above: an admin editing their own row is
    # how you lose your own access mid-session.
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot change your own role")

    # Demoting the last admin locks every human out of /admin; the only way
    # back is `python -m src.cli role:set` from a container shell. This branch
    # is unreachable over HTTP while the self-change guard above stands —
    # demoting the last admin X requires an actor with admin rights who is not
    # X, and if X is the last admin no such actor exists — but it stays as a
    # backstop for if that guard is ever relaxed, and it is what
    # tests/integration/test_role_appointment.py exercises by calling this
    # function directly with a non-admin actor.
    # access_status is part of the count on purpose. The invariant being
    # defended is "at least one admin can still LOG IN", and auth.py refuses a
    # user whose access_status is not 'allowed' — so a denied/pending admin is
    # not a way back in. Counting them anyway inflates the number, which makes
    # `<= 1` fire LESS often and therefore makes demotion EASIER: admins X
    # (denied) and Y (allowed) count as 2, Y is demotable, and zero loginable
    # admins remain. An earlier note recorded the unfiltered count as "more
    # conservative"; that was backwards.
    # ensure_admin_remains takes the admin-invariant lock, which is held until
    # the commit below, so concurrent demotions cannot both pass.
    if user_role != USER_ROLE_ADMIN:
        try:
            await ensure_admin_remains(db, user=user)
        except LastAdminError:
            raise HTTPException(
                status_code=400, detail="Cannot demote the last remaining admin"
            ) from None

    previous = user.user_role
    user.user_role = user_role
    if previous != user_role:
        # A role change signs the account out everywhere (spec 2026-10-01 §6.7).
        await bump_session_epoch(db, user.id)
    await db.commit()
    logger.info(
        "Admin %s changed role of %s (%s) from %s to %s",
        current_user.name, user.name, user_id, previous, user_role,
    )
    return RedirectResponse(url=f"/admin/users/{user_id}", status_code=302)


@router.post("/users/{user_id}/verify-email")
async def admin_verify_user_email(
    user_id: uuid.UUID,
    db: AsyncSession = _DB,
    current_user: User = _ADMIN,
):
    """Mark a user's email address verified (spec 2026-10-01 §6.6; any user).

    Refused under impersonation: the audit event must name the admin who vouched
    for the address, and under impersonation ``current_user`` is someone else.
    """
    refuse_impersonation(current_user, "Email verification is disabled while impersonating.")
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    error = await mark_email_verified(db, target=user, actor=current_user)
    if error:
        return RedirectResponse(url=f"/admin/users/{user_id}?error={error}", status_code=302)
    logger.info("Admin %s verified the email address of user %s", current_user.id, user_id)
    return RedirectResponse(url=f"/admin/users/{user_id}?email_verified=1", status_code=302)
