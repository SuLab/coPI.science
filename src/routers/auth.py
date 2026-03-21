"""ORCID OAuth flow — /login, /auth/callback, /logout."""

import logging
from datetime import datetime, timezone

from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.database import get_db
from src.models import Job, User
from src.services.orcid import fetch_orcid_profile

logger = logging.getLogger(__name__)
router = APIRouter()

ORCID_AUTH_URL = "https://orcid.org/oauth/authorize"
ORCID_TOKEN_URL = "https://orcid.org/oauth/token"
ORCID_SCOPE = "/authenticate"


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
    """Redirect to ORCID OAuth authorization page."""
    # If already logged in, redirect to home
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=302)

    settings = get_settings()
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

    # Verify state
    stored_state = request.session.pop("oauth_state", None)
    if stored_state and state != stored_state:
        logger.warning("OAuth state mismatch")
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

    # Find or create user
    result = await db.execute(select(User).where(User.orcid == orcid_id))
    user = result.scalar_one_or_none()

    if user is None:
        # Create new user
        user = User(
            orcid=orcid_id,
            name=profile_data.get("name") or orcid_name,
            email=profile_data.get("email"),
            institution=profile_data.get("institution"),
            department=profile_data.get("department"),
        )
        db.add(user)
        await db.flush()  # Get the ID

        # Enqueue profile generation job
        job = Job(
            type="generate_profile",
            user_id=user.id,
            payload={"user_id": str(user.id), "orcid": orcid_id},
        )
        db.add(job)
        logger.info("Created new user %s (%s) and enqueued profile job", user.id, orcid_id)
    else:
        # Existing user — update name/institution if empty
        if not user.name and profile_data.get("name"):
            user.name = profile_data["name"]
        if not user.institution and profile_data.get("institution"):
            user.institution = profile_data["institution"]
        if not user.department and profile_data.get("department"):
            user.department = profile_data["department"]
        if not user.email and profile_data.get("email"):
            user.email = profile_data["email"]
        # Set claimed_at if this was a seeded profile
        if user.claimed_at is None:
            user.claimed_at = datetime.now(timezone.utc)
        logger.info("Existing user %s logged in", user.id)

    await db.commit()

    # Set session
    request.session["user_id"] = str(user.id)

    # Redirect based on onboarding status
    if not user.onboarding_complete:
        return RedirectResponse(url="/onboarding", status_code=302)
    return RedirectResponse(url="/profile", status_code=302)


@router.post("/logout")
async def logout(request: Request):
    """Clear session and redirect to login."""
    request.session.clear()
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("copi-impersonate")
    return response


@router.get("/logout")
async def logout_get(request: Request):
    """GET logout for easy browser navigation."""
    request.session.clear()
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("copi-impersonate")
    return response
