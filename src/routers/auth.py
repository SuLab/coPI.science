"""ORCID OAuth flow — /login, /auth/callback, /logout."""

import logging
from datetime import datetime, timezone
from urllib.parse import urlsplit

from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.routing import Match

from fastapi.templating import Jinja2Templates

from src.config import get_settings
from src.database import get_db
from src.models import AccessAllowlist, Job, User
from src.services.orcid import fetch_orcid_profile

templates = Jinja2Templates(directory="templates")

logger = logging.getLogger(__name__)
router = APIRouter()

ORCID_AUTH_URL = "https://orcid.org/oauth/authorize"
ORCID_TOKEN_URL = "https://orcid.org/oauth/token"
ORCID_SCOPE = "/authenticate"

# Session key holding the path to resume after a successful login.
POST_LOGIN_KEY = "post_login_redirect"

# GET routes that resolve fine but must never be a post-login destination:
# the auth/session flow itself, and state-mutating GET links (e.g. the
# one-click unsubscribe). Matched as path prefixes.
_POST_LOGIN_DENY_PREFIXES = (
    "/login",
    "/logout",
    "/auth/",
    "/settings/unsubscribe/",
)


def _resolves_to_get_page(request: Request, path: str) -> bool:
    """True if ``path`` maps to a registered route that accepts GET.

    Delegates URL matching (including path params and mounts) to Starlette's
    own router so we accept exactly the pages the app actually serves, rather
    than guessing with string rules.
    """
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "path_params": {},
        "headers": [],
        "query_string": b"",
        "root_path": "",
        "app": request.app,
    }
    for route in request.app.router.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return True
    return False


def is_safe_next_url(request: Request, target: object) -> bool:
    """Guard a post-login redirect target.

    Two layers:
      1. Open-redirect defense — the value must be a purely relative,
         same-origin path. ``urlsplit`` does the parsing, so protocol-relative
         (``//host``), absolute (``https://host``) and scheme-only forms all
         surface a scheme/netloc and get rejected without hand-rolled string
         tricks. Backslashes (which some browsers fold to ``/``) and control
         chars are rejected outright.
      2. Valid-page check — the path must resolve to a real GET route and not
         be one of the auth/state-changing endpoints in the deny list.
    """
    if not isinstance(target, str) or not target or len(target) > 2000:
        return False
    if any(c in target for c in ("\r", "\n", "\x00", "\\")):
        return False

    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return False
    if not parts.path.startswith("/") or parts.path.startswith("//"):
        return False
    if any(parts.path.startswith(p) for p in _POST_LOGIN_DENY_PREFIXES):
        return False

    return _resolves_to_get_page(request, parts.path)


def pop_post_login_redirect(request: Request) -> str | None:
    """Return the stored post-login destination if present and safe."""
    target = request.session.pop(POST_LOGIN_KEY, None)
    return target if is_safe_next_url(request, target) else None


def _get_oauth_client() -> AsyncOAuth2Client:
    settings = get_settings()
    return AsyncOAuth2Client(
        client_id=settings.orcid_client_id,
        client_secret=settings.orcid_client_secret,
        redirect_uri=settings.orcid_redirect_uri,
        scope=ORCID_SCOPE,
    )


@router.get("/login", response_class=HTMLResponse)
async def login(request: Request):
    """Show the login landing page."""
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=302)
    # Remember where the user was headed so we can resume after ORCID auth.
    # Stashing in the (signed) session survives the external OAuth round-trip.
    next_url = request.query_params.get("next")
    if is_safe_next_url(request, next_url):
        request.session[POST_LOGIN_KEY] = next_url
    error = request.query_params.get("error")
    return templates.TemplateResponse(request, "login.html", {"error": error})


@router.get("/login/start")
async def login_start(request: Request):
    """Initiate ORCID OAuth redirect."""
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=302)
    client = _get_oauth_client()
    authorization_url, state = client.create_authorization_url(ORCID_AUTH_URL)
    request.session["oauth_state"] = state
    return RedirectResponse(url=authorization_url, status_code=302)


@router.get("/auth/callback")
async def auth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Handle ORCID OAuth callback."""
    if error:
        logger.warning("ORCID OAuth error: %s", error)
        return RedirectResponse(url="/login?error=oauth_error", status_code=302)

    if not code:
        return RedirectResponse(url="/login?error=no_code", status_code=302)

    # Verify state — FAIL CLOSED. The callback must present a state parameter
    # that matches the random value stashed in the (signed) session by
    # /login/start. Previously the check was skipped whenever the session held
    # no stored state, so a fresh/forged session hitting
    # /auth/callback?code=X&state=FORGED would sail through and log the victim
    # into the ATTACKER's ORCID identity (login CSRF). We now reject when the
    # stored state is missing, the inbound state is missing, or they differ.
    # ORCID's OAuth does not support PKCE (ORCID-Source#5977), so this state
    # parameter is the authorization-code CSRF defense.
    stored_state = request.session.pop("oauth_state", None)
    if not stored_state or not state or state != stored_state:
        logger.warning("OAuth state missing or mismatched — rejecting callback")
        return RedirectResponse(url="/login?error=state_mismatch", status_code=302)

    settings = get_settings()
    client = _get_oauth_client()

    try:
        token = await client.fetch_token(
            ORCID_TOKEN_URL,
            code=code,
            grant_type="authorization_code",
        )
    except Exception as exc:
        logger.error("Failed to fetch ORCID token: %s", exc)
        return RedirectResponse(url="/login?error=token_error", status_code=302)

    orcid_id = token.get("orcid")
    orcid_name = token.get("name", "")

    if not orcid_id:
        return RedirectResponse(url="/login?error=no_orcid", status_code=302)

    # Fetch full profile from ORCID API
    try:
        profile_data = await fetch_orcid_profile(orcid_id)
    except Exception as exc:
        logger.warning("Failed to fetch ORCID profile for %s: %s", orcid_id, exc)
        profile_data = {"orcid": orcid_id, "name": orcid_name}

    # Check the allowlist — ORCIDs on it bypass the pre-release access gate.
    # Keep the row (not just a bool): it also carries a fallback email used when
    # the ORCID public API exposes none (private by default).
    allowlist_result = await db.execute(
        select(AccessAllowlist).where(AccessAllowlist.orcid == orcid_id)
    )
    allowlist_entry = allowlist_result.scalar_one_or_none()
    is_allowlisted = allowlist_entry is not None

    # Resolve a best-effort email: prefer the ORCID-published address, else fall
    # back to the allowlist hint. Structured so more strategies can chain later.
    resolved_email = profile_data.get("email") or (
        allowlist_entry.email if allowlist_entry else None
    )

    # Find or create user
    result = await db.execute(select(User).where(User.orcid == orcid_id))
    user = result.scalar_one_or_none()

    if user is None:
        # Create new user — pending unless allowlisted
        user = User(
            orcid=orcid_id,
            name=profile_data.get("name") or orcid_name,
            email=resolved_email,
            institution=profile_data.get("institution"),
            department=profile_data.get("department"),
            access_status="allowed" if is_allowlisted else "pending",
        )
        db.add(user)
        await db.flush()  # Get the ID

        # Only enqueue profile generation for allowed users
        if user.access_status == "allowed":
            job = Job(
                type="generate_profile",
                user_id=user.id,
                payload={"user_id": str(user.id), "orcid": orcid_id},
            )
            db.add(job)
            logger.info("Created allowed user %s (%s), enqueued profile job", user.id, orcid_id)
        else:
            logger.info("Created pending user %s (%s) — awaiting admin approval", user.id, orcid_id)
    else:
        # Existing user — update name/institution if empty
        if not user.name and profile_data.get("name"):
            user.name = profile_data["name"]
        if not user.institution and profile_data.get("institution"):
            user.institution = profile_data["institution"]
        if not user.department and profile_data.get("department"):
            user.department = profile_data["department"]
        if not user.email and resolved_email:
            user.email = resolved_email
        # Allowlist can promote an existing pending user to allowed
        if is_allowlisted and user.access_status != "allowed":
            user.access_status = "allowed"
            from src.models import ResearcherProfile
            profile_check = await db.execute(
                select(ResearcherProfile.id).where(ResearcherProfile.user_id == user.id)
            )
            if profile_check.scalar_one_or_none() is None:
                db.add(
                    Job(
                        type="generate_profile",
                        user_id=user.id,
                        payload={"user_id": str(user.id), "orcid": orcid_id},
                    )
                )
        # Set claimed_at if this was a seeded profile
        if user.claimed_at is None:
            user.claimed_at = datetime.now(timezone.utc)
        logger.info("Existing user %s logged in (access=%s)", user.id, user.access_status)

    if user.access_status == "allowed":
        user.last_login_at = datetime.now(timezone.utc)

    await db.commit()

    # Access gate: users who aren't allowed do not get a session
    if user.access_status != "allowed":
        # Stash ORCID + any known email in session for the /access-pending page
        request.session["pending_access"] = {
            "user_id": str(user.id),
            "orcid": orcid_id,
            "email": user.email,
            "name": user.name,
        }
        return RedirectResponse(url="/access-pending", status_code=302)

    # Set session
    request.session["user_id"] = str(user.id)
    request.session.pop("pending_access", None)

    # Check for pending invite token — skip onboarding, go straight to acceptance
    pending_token = request.session.pop("pending_invite_token", None)
    if pending_token:
        request.session.pop(POST_LOGIN_KEY, None)
        return RedirectResponse(url=f"/invite/{pending_token}", status_code=302)

    # New PIs go through onboarding first; the saved destination is left in the
    # session and consumed when onboarding completes. A MANAGER is not a PI
    # (D7) and has no research profile to review, so it skips onboarding —
    # without this a manager is sent to a page whose only exit is saving a
    # research profile (POST /onboarding/save-profile is the sole write of
    # onboarding_complete in src/).
    #
    # Admins are excluded from the skip, not included in it: they keep the PI
    # surfaces (base.html still shows them My Profile / My Agent), so sending
    # an admin with incomplete onboarding anywhere but /onboarding just defers
    # the same bounce one hop, via /profile.
    if not user.onboarding_complete and not user.is_manager:
        return RedirectResponse(url="/onboarding", status_code=302)

    # Resume the page the user originally requested, if any.
    next_url = pop_post_login_redirect(request)
    if next_url:
        return RedirectResponse(url=next_url, status_code=302)
    if user.is_manager:
        return RedirectResponse(url="/manager/pis", status_code=302)
    return RedirectResponse(url="/profile", status_code=302)


@router.post("/logout")
async def logout(request: Request):
    """Clear session and redirect to login.

    POST-only: logout mutates session state, so exposing it over GET made it a
    cross-site request-forgery target (a third-party page could log a victim
    out via an <img>/<a> to /logout). SameSite=lax on the session cookie blocks
    forged cross-site POSTs, so the "Sign out" control posts this form
    (see base.html). (SEC-8)
    """
    request.session.clear()
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("copi-impersonate")
    return response
