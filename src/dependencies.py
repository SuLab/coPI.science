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

logger = logging.getLogger(__name__)


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


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Auth dependency. Checks session cookie for user_id.
    Handles impersonation via copi-impersonate cookie (admin only).
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

    # Revocation. Sessions are unkeyed signed cookies with a 30-day max_age and
    # no server-side store, so there is no session to invalidate and
    # `access_status` is the ONLY revocation signal there is. Nothing read it
    # after login, so admin_deny_access set the column and changed nothing a
    # signed-in user could observe: a denied user's GET /profile returned 200
    # for up to thirty more days (E1.2).
    #
    # Checked on `session_user`, the account that actually holds the session —
    # deliberately BEFORE the impersonation block below, and never on the
    # impersonated user. CONSEQUENCE, INTENTIONAL AND RULED ON: an admin can
    # still impersonate a user whose access_status is 'denied' or 'pending'.
    # That is a support path (looking at a blocked account is how you find out
    # why it is blocked), not a hole — the admin's own session is what is being
    # authorised here, and it is 'allowed'. Do not "fix" it by moving this
    # check below the impersonation block.
    #
    # POP `user_id`; do NOT call request.session.clear(). /access-pending
    # renders `session["pending_access"]`, so clearing the session lands the
    # user on a page that cannot say which account is blocked. We repopulate
    # `pending_access` in exactly the shape src/routers/auth.py's own access
    # gate writes at login, so the two arrivals at that page look the same.
    #
    # This redirect target and POST /logout must both stay free of
    # get_current_user: /access-pending (src/routers/public.py) takes no auth
    # dependency and POST /logout (src/routers/auth.py) takes none either.
    # Adding one to either turns this bounce into a loop with no way out — see
    # tests/integration/test_access_revocation.py.
    if session_user.access_status != "allowed":
        request.session.pop("user_id", None)
        request.session["pending_access"] = {
            "user_id": str(session_user.id),
            "orcid": session_user.orcid,
            "email": session_user.email,
            "name": session_user.name,
        }
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/access-pending"},
        )

    # Impersonation: admin can view as another user
    impersonate_id = request.cookies.get("copi-impersonate")
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


async def get_pi_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Dependency for the PI-owned *write* surfaces: rejects a manager.

    D7 makes manager and PI mutually exclusive, and a manager has no lab of
    its own. Read-only bounces (auth.py's post-login redirect,
    onboarding.py's GET) are not enough on their own: the writes are what
    actually mint a lab. POST /onboarding/save-profile is the ONLY writer of
    `onboarding_complete = True` in src/ and it also creates the
    ResearcherProfile, and POST /agent/request gates solely on those two — so
    a manager who could POST the first could then POST the second and receive
    an AgentRegistry row. That is the escalation this closes.

    The predicate is `is_manager`, NOT `user_role == 'pi'`: an admin is not a
    `pi` either, and `templates/base.html` still offers admins the My Profile
    and My Agent links, so a `== 'pi'` test would 403 every admin on their own
    nav. Admins keep the PI surfaces, exactly as they did before this branch.

    403 rather than a redirect: every route wearing this is a POST, and
    replaying a POST as a GET navigation is wrong for the same reason
    `_login_location` above refuses to remember one. A manager never sees
    these forms (the nav hides them), so reaching one is not a wrong turn to
    be gently corrected — it is a request that must simply fail, visibly.
    """
    if current_user.is_manager:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Managers have no lab profile or agent (PI accounts only)",
        )
    return current_user


async def get_staff_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Dependency that requires admin OR manager.

    Used ONLY by the /manager router. This is deliberately a separate
    dependency rather than a relaxation of get_admin_user: /admin declares its
    gate on 34 individual handlers (F5), and widening the one they share is how
    a read-only role would quietly acquire write endpoints.

    Note this also 403s an admin who is currently impersonating a PI, because
    get_current_user returns the impersonated user. That is correct.
    """
    if not current_user.is_staff:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Manager access required"
        )
    return current_user
