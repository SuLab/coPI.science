"""FastAPI dependencies for auth and DB access."""

import logging
import uuid
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.database import get_db
from src.models import User
from src.services.session_epoch import current_epoch, epoch_in_session

logger = logging.getLogger(__name__)

IMPERSONATE_COOKIE = "copi-impersonate"


def _login_location(request: Request) -> str:
    """Build the /login redirect, remembering where the user was headed.

    Only GET navigations to a real page are worth resuming after sign-in, so
    we skip POSTs (replaying them as a GET would be wrong) and the login/root
    pages (no point looping back to them). The destination is consumed and
    re-validated in auth.py once the ORCID round-trip completes.
    """
    if request.method != "GET":
        return "/login"
    target = request.url.path
    if request.url.query:
        target += "?" + request.url.query
    if target in ("/", "/login"):
        return "/login"
    return f"/login?next={quote(target, safe='')}"


async def session_account(request: Request, db: AsyncSession) -> User:
    """The account holding the session cookie, never an impersonated one.

    Raises the 302 to /login when there is no session user, the id is malformed or
    unknown, the account is not allowed, or the session's epoch is stale.
    """
    user_id_str = request.session.get("user_id")
    if not user_id_str:
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": _login_location(request)},
        )

    try:
        user_id = uuid.UUID(user_id_str)
    except ValueError:
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/login"},
        )

    result = await db.execute(
        select(User).options(selectinload(User.profile)).where(User.id == user_id)
    )
    session_user = result.scalar_one_or_none()

    if session_user is None:
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/login"},
        )

    # Re-check the access gate on every request, not only when the session is
    # minted. auth.py gates at sign-in (it refuses to set session["user_id"] for
    # a non-allowed user), so revoking access mid-session used to do nothing: a
    # denied user kept a fully working session until the cookie expired. Clearing
    # the session gets the same treatment as the stale-user_id paths above — the
    # /login round-trip then lands them on /access-pending.
    #
    # Deliberately BEFORE the impersonation block: this guards the *signed-in*
    # identity, and admins impersonating a pending/denied user to debug their
    # account is a supported workflow that must keep working.
    if session_user.access_status != "allowed":
        logger.info(
            "Bounced session for user %s with access_status=%s",
            session_user.id,
            session_user.access_status,
        )
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/login"},
        )

    # The epoch belongs to the account holding the session, so it is checked here,
    # before impersonation: an admin whose own session was revoked (logout, the
    # user's unlink, the admin's removal bump it) loses the impersonation with it.
    if epoch_in_session(request.session) != current_epoch(session_user):
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": _login_location(request)},
        )

    return session_user


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Auth dependency: the session account (see session_account).
    Handles impersonation via copi-impersonate cookie (admin only).
    """
    session_user = await session_account(request, db)

    # Impersonation: admin can view as another user
    impersonate_id = request.cookies.get(IMPERSONATE_COOKIE)
    if impersonate_id and session_user.is_admin:
        try:
            imp_uuid = uuid.UUID(impersonate_id)
            result = await db.execute(
                select(User).options(selectinload(User.profile)).where(User.id == imp_uuid)
            )
            imp_user = result.scalar_one_or_none()
            if imp_user:
                # Tag so templates can show impersonation banner
                imp_user._is_impersonated = True  # type: ignore[attr-defined]
                imp_user._real_admin = session_user  # type: ignore[attr-defined]
                return imp_user
        except (ValueError, Exception) as exc:
            logger.warning("Invalid impersonate cookie: %s", exc)

    return session_user


def refuse_impersonation(current_user: User, detail: str) -> None:
    """403 when the session is impersonating. `detail` names the refused action."""
    if getattr(current_user, "_is_impersonated", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def account_of(current_user: User) -> User:
    """The account that holds the session: the real admin when impersonating."""
    if getattr(current_user, "_is_impersonated", False):
        return current_user._real_admin  # type: ignore[attr-defined]
    return current_user


def impersonating(request: Request, account: User) -> bool:
    """True when an admin's request carries an impersonation cookie, even one that
    names no user (get_current_user then falls back to the admin's own account)."""
    return bool(account.is_admin) and bool(request.cookies.get(IMPERSONATE_COOKIE))


async def get_agent_with_access(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> tuple["AgentRegistry", bool]:
    """
    Load agent by agent_id slug. Verify user is PI or active delegate.
    Returns (agent, is_owner) tuple. is_owner=True means PI, False means delegate.
    Raises 403 if neither.
    """
    from src.models import AgentDelegate, AgentRegistry

    result = await db.execute(
        select(AgentRegistry).where(AgentRegistry.agent_id == agent_id)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Check if PI
    if agent.user_id == current_user.id:
        return agent, True

    # Check if delegate
    delegate_result = await db.execute(
        select(AgentDelegate.id).where(
            AgentDelegate.agent_registry_id == agent.id,
            AgentDelegate.user_id == current_user.id,
        )
    )
    if delegate_result.scalar_one_or_none():
        return agent, False

    raise HTTPException(status_code=403, detail="Access denied")


async def get_admin_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Dependency that requires admin status."""
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user
