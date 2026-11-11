"""ORCID OAuth flow — /login, /auth/callback, /logout — and the sign-in tail every provider
shares (complete_sign_in)."""

import logging
import secrets
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.routing import Match

from src.config import get_settings
from src.database import get_db
from src.dependencies import (
    IMPERSONATE_COOKIE,
    account_of,
    get_current_user,
    impersonating,
)
from src.models import AccessAllowlist, Job, User
from src.services.id_tokens import IdTokenError
from src.services.microsoft_identity import message_for
from src.services.orcid import fetch_orcid_profile
from src.services.orcid_reauth import validate_reauth_id_token
from src.services.session_epoch import (
    SESSION_EPOCH_KEY,
    bump_session_epoch,
    current_epoch,
    epoch_in_session,
)
from src.sign_in_session import VIA_ORCID, record_orcid_reauth, record_sign_in
from src.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter()
# Module-level dependency singleton (no Depends(...) in an argument default; the
# src/ lint ratchet counts each one).
_DB = Depends(get_db)

ORCID_AUTH_URL = "https://orcid.org/oauth/authorize"
ORCID_TOKEN_URL = "https://orcid.org/oauth/token"
ORCID_SCOPE = "/authenticate"
# The ORCID re-sign-in before linking a Microsoft account: ORCID documents `prompt` and
# `auth_time` only for the openid scope, which grants what /authenticate does.
ORCID_REAUTH_SCOPE = "openid"
# Session key of a pending reauth: {"nonce", "started_at"}.
ORCID_REAUTH_KEY = "orcid_reauth"
REAUTH_FLOW_MAX_AGE_SECONDS = 600
REAUTH_FAILED_URL = "/settings?microsoft=reauth_failed"

# Session key holding the path to resume after a successful login.
POST_LOGIN_KEY = "post_login_redirect"

# The remembered destination rides in the session cookie, and the redirect to Microsoft carries
# that cookie through nginx's 4 KB upstream-header buffer. Capping the length and allowing only
# printable ASCII other than `"` keeps its JSON encoding at most MAX_NEXT_URL_CHARS + 2 bytes
# (control characters would grow 6x there, `"` 2x).
MAX_NEXT_URL_CHARS = 1024

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
         tricks. Backslashes (which some browsers fold to ``/``), control
         chars, non-ASCII, spaces and ``"`` are rejected outright, and the
         length is capped (see MAX_NEXT_URL_CHARS).
      2. Valid-page check — the path must resolve to a real GET route and not
         be one of the auth/state-changing endpoints in the deny list.
    """
    if not isinstance(target, str) or not target or len(target) > MAX_NEXT_URL_CHARS:
        return False
    if any(c in target for c in ("\r", "\n", "\x00", "\\")):
        return False
    if any(not ("!" <= c <= "~") or c == '"' for c in target):
        return False
    # "///route" splits to an empty netloc and the path "/route", but browsers resolve it as
    # the host "route": refuse any target that starts like a network path.
    if target.startswith("//"):
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


def _start_fresh_session(request: Request) -> None:
    """Clear the session at login, keeping only what the login itself must carry.

    Nothing from a session that predates the login — an anonymous visitor's, or one
    planted by someone else (session fixation) — reaches the signed-in session: the
    post-login destination is re-stored only if it passes is_safe_next_url, and a
    pending delegate-invite token survives so the invite flow (src/routers/invite.py)
    can resume after sign-in. Every other key is dropped.
    """
    next_url = pop_post_login_redirect(request)
    pending_token = request.session.get("pending_invite_token")
    request.session.clear()
    if next_url:
        request.session[POST_LOGIN_KEY] = next_url
    if isinstance(pending_token, str) and pending_token:
        request.session["pending_invite_token"] = pending_token


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
    message = message_for(error)
    return templates.TemplateResponse(request, "login.html", {
        "error": error,
        "error_message": message[0] if message and message[1] == "error" else None,
        "notice": message[0] if message and message[1] in ("info", "success") else None,
        "microsoft_enabled": getattr(request.app.state, "microsoft_sign_in", None) is not None,
    })


@router.get("/login/start")
async def login_start(request: Request, reauth: str | None = None, db: AsyncSession = _DB):
    """Initiate the ORCID OAuth redirect.

    With ``reauth=1`` a signed-in user signs in to ORCID again before linking a Microsoft
    account: ``prompt=login`` forces the password prompt and the callback checks the ID token's
    ``auth_time``, then returns to /settings. Refused while impersonating.
    """
    signed_in = bool(request.session.get("user_id"))
    if signed_in and reauth != "1":
        return RedirectResponse(url="/", status_code=302)
    client = _get_oauth_client()
    if signed_in:
        # A stale or revoked session is bounced here, as on any page (the 302 propagates).
        current = await get_current_user(request, db)
        if impersonating(request, account_of(current)):
            return RedirectResponse(url="/settings?microsoft=impersonating", status_code=302)
        nonce = secrets.token_urlsafe(32)
        authorization_url, state = client.create_authorization_url(
            ORCID_AUTH_URL, scope=ORCID_REAUTH_SCOPE, prompt="login", nonce=nonce
        )
        request.session[ORCID_REAUTH_KEY] = {"nonce": nonce, "started_at": int(time.time())}
        request.session[POST_LOGIN_KEY] = "/settings"
    else:
        # An abandoned reauth left in a since-revoked session would turn this ordinary
        # sign-in's callback into a reauth that has no signed-in user to prove.
        request.session.pop(ORCID_REAUTH_KEY, None)
        authorization_url, state = client.create_authorization_url(ORCID_AUTH_URL)
    request.session["oauth_state"] = state
    return RedirectResponse(url=authorization_url, status_code=302)


def _post_login_redirect(request: Request, user: User, *, via: str) -> RedirectResponse:
    """Where a freshly logged-in, allowed user goes; writes the session keys, including the
    sign-in stamp (which provider, and when) that linking a Microsoft account checks."""
    # The epoch is what get_current_user compares with users.session_epoch on every request.
    request.session["user_id"] = str(user.id)
    request.session[SESSION_EPOCH_KEY] = current_epoch(user)
    record_sign_in(request.session, via)
    request.session.pop("pending_access", None)

    # Check for pending invite token — skip onboarding, go straight to acceptance
    pending_token = request.session.pop("pending_invite_token", None)
    if pending_token:
        request.session.pop(POST_LOGIN_KEY, None)
        return RedirectResponse(url=f"/invite/{pending_token}", status_code=302)

    # New users go through onboarding first; the saved destination is left in
    # the session and consumed when onboarding completes.
    if not user.onboarding_complete:
        return RedirectResponse(url="/onboarding", status_code=302)

    # Resume the page the user originally requested, if any.
    next_url = pop_post_login_redirect(request)
    return RedirectResponse(url=next_url or "/profile", status_code=302)


async def complete_sign_in(
    request: Request, db: AsyncSession, user: User, *, via: str
) -> RedirectResponse:
    """The end of every sign-in, ORCID or Microsoft: stamp, commit, start a fresh session, then
    the access gate and the landing page. One function, so the gate and the session rules cannot
    drift between providers. The response also deletes the impersonation cookie, so a new
    sign-in does not inherit an impersonation."""
    # The session carries the epoch read when the user was looked up, before this commit: a
    # revocation (a logout elsewhere, an unlink, an admin's removal) that commits in between
    # then leaves the new session stale instead of handing it the bumped epoch. Only a user
    # created in this request has none loaded (the column has no default); its row is new, so
    # nothing can have bumped it, and it is read after the commit because an async session
    # cannot lazy-load it in _post_login_redirect.
    epoch_loaded = "session_epoch" not in sa_inspect(user).unloaded
    if user.access_status == "allowed":
        user.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    if not epoch_loaded:
        await db.refresh(user, ["session_epoch"])

    _start_fresh_session(request)

    # Access gate: users who aren't allowed do not get a session
    if user.access_status != "allowed":
        # Stash ORCID + any known email in session for the /access-pending page
        request.session["pending_access"] = {
            "user_id": str(user.id),
            "orcid": user.orcid,
            "email": user.email,
            "name": user.name,
        }
        response = RedirectResponse(url="/access-pending", status_code=302)
    else:
        response = _post_login_redirect(request, user, via=via)
    response.delete_cookie(IMPERSONATE_COOKIE)
    return response


def _callback_failure(request: Request, code: str, *, reauth: bool) -> RedirectResponse:
    """Where a failed ORCID callback lands. A reauth belongs to a signed-in user, whom /login
    would bounce to / and drop the message, so it goes back to /settings instead."""
    if reauth:
        request.session.pop(POST_LOGIN_KEY, None)
        return RedirectResponse(url=REAUTH_FAILED_URL, status_code=302)
    return RedirectResponse(url=f"/login?error={code}", status_code=302)


async def _orcid_reauth_refusal(
    request: Request, db: AsyncSession, token: dict, orcid_id: str, reauth: dict
) -> RedirectResponse | None:
    """None when this callback proves the signed-in user just re-entered their ORCID
    credentials; otherwise the refusal, with the session left as it was."""
    now = int(time.time())
    user_id = _session_user_id(request)
    user = await db.get(User, user_id) if user_id is not None else None
    started = reauth.get("started_at")
    if (user is None or user.orcid != orcid_id or impersonating(request, user)
            or isinstance(started, bool) or not isinstance(started, int)
            or now - started > REAUTH_FLOW_MAX_AGE_SECONDS):
        logger.warning("ORCID reauth refused: not the signed-in account, impersonating, or expired")
        return _callback_failure(request, "reauth", reauth=True)
    try:
        await validate_reauth_id_token(
            token.get("id_token"), client_id=get_settings().orcid_client_id,
            nonce=reauth.get("nonce"), orcid=orcid_id, now=now,
        )
    except IdTokenError as exc:
        logger.warning("ORCID reauth refused: %s", exc)
        return _callback_failure(request, "reauth", reauth=True)
    return None


@router.get("/auth/callback")
async def auth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = _DB,
):
    """Handle the ORCID OAuth callback: a sign-in, or the re-sign-in /login/start?reauth=1
    began, which must prove itself (_orcid_reauth_refusal) before it signs in afresh."""
    reauth = request.session.pop(ORCID_REAUTH_KEY, None)
    is_reauth = isinstance(reauth, dict)
    if error:
        # %r and a cap: the value comes straight from the query string and may carry line
        # breaks that would otherwise forge log lines.
        logger.warning("ORCID OAuth error: %r", error[:80])
        return _callback_failure(request, "oauth_error", reauth=is_reauth)

    if not code:
        return _callback_failure(request, "no_code", reauth=is_reauth)

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
        return _callback_failure(request, "state_mismatch", reauth=is_reauth)

    client = _get_oauth_client()

    try:
        token = await client.fetch_token(
            ORCID_TOKEN_URL,
            code=code,
            grant_type="authorization_code",
        )
    except Exception as exc:
        logger.error("Failed to fetch ORCID token: %s", exc)
        return _callback_failure(request, "token_error", reauth=is_reauth)

    orcid_id = token.get("orcid")
    orcid_name = token.get("name", "")

    if not orcid_id:
        return _callback_failure(request, "no_orcid", reauth=is_reauth)

    if is_reauth:
        refusal = await _orcid_reauth_refusal(request, db, token, orcid_id, reauth)
        if refusal is not None:
            return refusal

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

    response = await complete_sign_in(request, db, user, via=VIA_ORCID)
    if is_reauth and request.session.get("user_id"):
        # complete_sign_in started a fresh session; only now may it carry the proof.
        record_orcid_reauth(request.session)
    return response


def _session_user_id(request: Request) -> uuid.UUID | None:
    """``session["user_id"]`` as a UUID; None when absent or malformed."""
    raw = request.session.get("user_id")
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


@router.post("/logout")
async def logout(request: Request, db: AsyncSession = _DB):
    """Sign out on every device and redirect to login.

    POST-only: logout mutates session state, so exposing it over GET made it a
    cross-site request-forgery target (a third-party page could log a victim
    out via an <img>/<a> to /logout). SameSite=lax on the session cookie blocks
    forged cross-site POSTs, so the "Sign out" control posts this form
    (see base.html).

    Bumping ``users.session_epoch`` makes get_current_user refuse every other
    session of the account. The account comes from ``session["user_id"]`` directly,
    never from get_current_user: a denied user is bounced from every authenticated route
    to /access-pending and must still be able to sign out, and an auth dependency here
    would turn that bounce into a loop. Only a session still holding the current epoch
    bumps it, so replaying an already-revoked cookie cannot sign the account out of its
    newer sessions.
    """
    user_id = _session_user_id(request)
    epoch = epoch_in_session(request.session)
    if user_id is not None and epoch is not None:
        await bump_session_epoch(db, user_id, expected_epoch=epoch)
        await db.commit()
    request.session.clear()
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(IMPERSONATE_COOKIE)
    return response
