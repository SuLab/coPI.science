"""FastAPI dependencies for auth and DB access."""

import logging
import time
import uuid
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.database import get_db
from src.models import User
from src.services.session_epoch import current_epoch, epoch_in_session

logger = logging.getLogger(__name__)

#: Session keys holding an admin's impersonation (spec 2026-10-01 §6.7). They live
#: in the signed session, so only POST /admin/impersonate writes them; the old
#: unsigned ``copi-impersonate`` cookie is neither read nor written.
IMPERSONATE_KEY = "impersonate_user_id"
IMPERSONATE_EXPIRES_KEY = "impersonate_expires_at"
#: An impersonation lapses this many seconds after it starts: the 24 h the old
#: cookie's max_age gave it.
IMPERSONATION_MAX_AGE = 24 * 3600


def _login_location(request: Request) -> str:
    """Build the /login redirect, remembering where the user was headed.

    Only GET navigations to a real page are worth resuming after sign-in, so
    we skip POSTs (replaying them as a GET would be wrong), the login/root
    pages (no point looping back to them), and anything under
    /assessment-chat/ (those are JSON endpoints the drawer polls in the
    background, not a page to resume — remembering one as `next` would land a
    freshly signed-in user on raw JSON). The destination is consumed and
    re-validated in auth.py once the ORCID round-trip completes.
    """
    if request.method != "GET":
        return "/login"
    target = request.url.path
    if target.startswith("/assessment-chat/"):
        return "/login"
    if request.url.query:
        target += "?" + request.url.query
    if target in ("/", "/login"):
        return "/login"
    return f"/login?next={quote(target, safe='')}"


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Auth dependency: the user the signed session holds, or a 302 to /login or
    /access-pending.

    For an admin session holding an impersonation (``IMPERSONATE_KEY``, set by
    POST /admin/impersonate), returns the impersonated user, tagged for the banner.
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

    # Revocation by access status. Sessions are signed cookies with a 30-day
    # max_age and no server-side store, and nothing read `access_status` after
    # login, so admin_deny_access set the column and changed nothing a signed-in
    # user could observe: a denied user's GET /profile returned 200 for up to
    # thirty more days (E1.2). The session-epoch check below is the other
    # revocation signal.
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

    # Revocation by session epoch (spec 2026-10-01 §6.7). Login copies
    # users.session_epoch into the session; logout, access denial and a role
    # change bump it, so a session holding any other epoch is over, on every
    # device. After the access check above, so a denied user still lands on
    # /access-pending; before impersonation, because the epoch belongs to the
    # account that holds the session.
    if epoch_in_session(request.session) != current_epoch(session_user):
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": _login_location(request)},
        )

    # Impersonation: admin can view as another user
    impersonated = await _impersonated_user(request, db, session_user)
    return impersonated if impersonated is not None else session_user


def start_impersonation(request: Request, target_id: uuid.UUID) -> None:
    """Record in the signed session that this admin session views the site as
    ``target_id``, for IMPERSONATION_MAX_AGE seconds."""
    request.session[IMPERSONATE_KEY] = str(target_id)
    request.session[IMPERSONATE_EXPIRES_KEY] = int(time.time()) + IMPERSONATION_MAX_AGE


def end_impersonation(request: Request) -> None:
    """Drop the session's impersonation, if it holds one."""
    request.session.pop(IMPERSONATE_KEY, None)
    request.session.pop(IMPERSONATE_EXPIRES_KEY, None)


async def _impersonated_user(
    request: Request, db: AsyncSession, session_user: User
) -> User | None:
    """The user an admin session is viewing as, tagged for the banner; None when the
    session is not impersonating, its holder is not an admin, the impersonation has
    lapsed or is malformed (both dropped from the session), or the target is gone."""
    impersonate_id = request.session.get(IMPERSONATE_KEY)
    if impersonate_id and session_user.is_admin:
        expires_at = request.session.get(IMPERSONATE_EXPIRES_KEY)
        if not isinstance(expires_at, int) or expires_at <= time.time():
            end_impersonation(request)
            return None
        try:
            imp_uuid = uuid.UUID(str(impersonate_id))
        except ValueError:
            logger.warning("Invalid impersonate_user_id in session: %r", impersonate_id)
            end_impersonation(request)
            return None
        result = await db.execute(
            select(User).options(selectinload(User.profile)).where(User.id == imp_uuid)
        )
        imp_user = result.scalar_one_or_none()
        if imp_user:
            # Tag so templates can show impersonation banner
            imp_user._is_impersonated = True  # type: ignore[attr-defined]
            imp_user._real_admin = session_user  # type: ignore[attr-defined]
            if request.method not in ("GET", "HEAD"):
                # A-10: the one impersonation note every write gets, whatever
                # table it lands in. Tables with a recorded_by column or a
                # revision summary also carry it (impersonation_note below);
                # for the rest (jobs, agents, cohort audit, allowlist,
                # delegate invitations) this line is the record.
                logger.warning(
                    "Write %s %s by admin %s while impersonating %s",
                    request.method, request.url.path, session_user.id, imp_user.id,
                )
            return imp_user
    return None


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
    """Dependency for the PI-owned *write* surfaces: rejects a manager or a
    reviewer.

    D7 makes manager and PI mutually exclusive, and a manager has no lab of
    its own; a reviewer is neither staff nor PI and has no lab
    either. Read-only bounces (auth.py's post-login redirect, onboarding.py's
    GET) are not enough on their own: the writes are what actually mint a
    lab. POST /onboarding/save-profile is the ONLY writer of
    `onboarding_complete = True` in src/ and it also creates the
    ResearcherProfile, and POST /agent/request gates solely on those two — so
    a manager or reviewer who could POST the first could then POST the second
    and receive an AgentRegistry row. That is the escalation this closes.

    The predicate is `User.may_use_pi_surfaces` (PI or admin), NOT
    `user_role == 'pi'`: an admin is not a `pi` either, and
    `templates/base.html` still offers admins the My Profile and My Agent
    links, so a `== 'pi'` test would 403 every admin on their own nav. It
    equals the old `not (is_manager or is_reviewer)` for every role in
    VALID_USER_ROLES, and an unknown role fails closed.

    403 rather than a redirect: every route wearing this is a POST, and
    replaying a POST as a GET navigation is wrong for the same reason
    `_login_location` above refuses to remember one. Neither role ever sees
    these forms (the nav hides them), so reaching one is not a wrong turn to
    be gently corrected — it is a request that must simply fail, visibly.
    """
    if not current_user.may_use_pi_surfaces:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Staff accounts have no lab profile or agent (PI accounts only)",
        )
    return current_user


def staff_landing_redirect(user: User) -> RedirectResponse | None:
    """Where a GET of a PI-only page sends an account with no lab (M-08): a
    manager to /manager/pis, a reviewer to /manager/assessments. None for a PI or
    an admin (``User.may_use_pi_surfaces``). The PI-only POSTs keep get_pi_user's
    403; this is the navigation half."""
    if user.may_use_pi_surfaces:
        return None
    return RedirectResponse(
        url="/manager/pis" if user.is_manager else "/manager/assessments", status_code=302
    )


def refuse_impersonation(
    current_user: User, detail: str = "Review actions are disabled while impersonating."
) -> None:
    """403 when the session is impersonating. `detail` names the refused action."""
    if getattr(current_user, "_is_impersonated", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


def recorded_by(current_user: User) -> User | None:
    """The real admin when the session is impersonating, else None. Writes are
    attributed to the impersonated user (operator decision 2026-09-10); this is
    the second signature, for tables that have a recorded_by column."""
    if getattr(current_user, "_is_impersonated", False):
        real = getattr(current_user, "_real_admin", None)
        logger.warning(
            "Action by admin %s while impersonating %s", getattr(real, "id", None), current_user.id
        )
        return real
    return None


def impersonation_note(current_user: User) -> str | None:
    """For writes with no recorded_by column: a note for the log line and the
    revision's change_summary."""
    real = recorded_by(current_user)
    return f"impersonated by admin {real.id}" if real is not None else None


async def get_staff_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Dependency that requires admin OR manager.

    Used by the /manager router, the /reviews router's staff-only routes, and
    the /admin Slack OAuth callback (``admin_provision_slack_callback``, widened
    from admin to staff under F2).
    This is deliberately a separate dependency rather than a relaxation of
    get_admin_user: /admin declares its gate on 39 individual handlers (F5),
    and widening the one they share is how a read-only role would quietly
    acquire write endpoints.

    Note this also 403s an admin who is currently impersonating a PI, because
    get_current_user returns the impersonated user. That is correct.
    """
    if not current_user.is_staff:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Manager access required"
        )
    return current_user


async def get_review_user(current_user: User = Depends(get_current_user)) -> User:
    """Admin, manager, or reviewer. Gates the /reviews router and the manager
    router's reviewer-visible GETs. Deliberately separate from get_staff_user:
    is_staff keeps gating manager writes, discussions, activity and prompt
    suggestions, which a reviewer must never reach."""
    if not (current_user.is_staff or current_user.is_reviewer):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Review access required")
    return current_user
