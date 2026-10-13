"""Microsoft sign-in for staff: start, link and the one shared callback (spec
docs/specs/2026-10-13-microsoft-staff-sign-in-design.md §5.3-§5.6).

Every handler 404s while app.state.microsoft_sign_in is None (feature off). The callback is a
GET (response_mode=query): a form_post callback would be refused by OriginGuardMiddleware and
arrive without the SameSite=lax session cookie. One session key, FLOW_KEY, carries a single
attempt's state, nonce, PKCE verifier and purpose; it is separate from ORCID's oauth_state, so
neither provider's callback can consume the other's flow.
"""

from __future__ import annotations

import logging
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import IMPERSONATE_KEY, get_current_user, refuse_impersonation
from src.models import User
from src.routers.auth import complete_sign_in
from src.services import microsoft_identity
from src.services.id_tokens import IdTokenError
from src.services.microsoft_identity import (
    MICROSOFT_ERROR_MESSAGES,
    InteractionRequired,
    MicrosoftIdentity,
    MicrosoftSignInConfig,
    MicrosoftSignInError,
    may_use_microsoft_sign_in,
)
from src.web.flash import flash
from src.web.sign_in_session import VIA_MICROSOFT, fresh_orcid_sign_in

logger = logging.getLogger(__name__)
router = APIRouter()

FLOW_KEY = "microsoft_flow"
FLOW_MAX_AGE_SECONDS = 600
_IMPERSONATION_DETAIL = "Microsoft sign-in changes are disabled while impersonating."


def _require_config(request: Request) -> MicrosoftSignInConfig:
    config = getattr(request.app.state, "microsoft_sign_in", None)
    if config is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return config


# Module-level dependency singletons (the src/ lint ratchet counts each Depends in a default).
_CONFIG = Depends(_require_config)
_DB = Depends(get_db)
_USER = Depends(get_current_user)


def _fail(request: Request, purpose: object, code: str) -> RedirectResponse:
    """A linking user (signed in) gets a flash on /settings; anyone else /login?error=<code>,
    because /login sends a signed-in user to / and would drop the message."""
    if purpose == "link" or request.session.get("user_id"):
        flash(request, MICROSOFT_ERROR_MESSAGES[code], "error")
        return RedirectResponse(url="/settings", status_code=302)
    return RedirectResponse(url=f"/login?error={code}", status_code=302)


def _start(request: Request, config: MicrosoftSignInConfig, *, purpose: str,
           user_id: str | None = None) -> RedirectResponse:
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)  # 86 characters, within PKCE's 43-128
    request.session[FLOW_KEY] = {
        "state": state, "nonce": nonce, "code_verifier": code_verifier, "purpose": purpose,
        "user_id": user_id, "started_at": int(time.time()),
    }
    url = microsoft_identity.authorization_url(
        config, state=state, nonce=nonce, code_verifier=code_verifier,
        select_account=purpose == "link",
    )
    return RedirectResponse(url=url, status_code=302)


@router.get("/auth/microsoft/start")
async def microsoft_start(request: Request, config: MicrosoftSignInConfig = _CONFIG):
    """Begin a Microsoft sign-in from the /login button."""
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=302)
    return _start(request, config, purpose="signin")


@router.get("/auth/microsoft/link")
async def microsoft_link_start(request: Request, config: MicrosoftSignInConfig = _CONFIG,
                               current_user: User = _USER):
    """Begin linking from Settings: staff only, not impersonating, proven ORCID re-sign-in
    ≤ 15 min old (fresh_orcid_sign_in)."""
    refuse_impersonation(current_user, _IMPERSONATION_DETAIL)
    if not may_use_microsoft_sign_in(current_user):
        raise HTTPException(status_code=403, detail="Microsoft sign-in is for staff accounts.")
    if not fresh_orcid_sign_in(request.session):
        flash(request, MICROSOFT_ERROR_MESSAGES["microsoft_link_refused"], "error")
        return RedirectResponse(url="/settings", status_code=302)
    return _start(request, config, purpose="link", user_id=str(current_user.id))


@router.get("/auth/microsoft/callback")
async def microsoft_callback(
    request: Request,
    config: MicrosoftSignInConfig = _CONFIG,
    db: AsyncSession = _DB,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Microsoft's redirect back, for a sign-in or a link (the flow says which)."""
    flow = request.session.pop(FLOW_KEY, None)
    if not isinstance(flow, dict):
        return _fail(request, None, "state_mismatch")
    purpose = flow.get("purpose")
    if error:
        # %r, not %s: both values come from the query string and may carry line breaks.
        logger.warning("Microsoft sign-in error=%r description=%r", error[:80],
                       request.query_params.get("error_description", "")[:300])
        return _fail(request, purpose, "microsoft_cancelled" if error == "access_denied" else "microsoft_error")
    started_at = flow.get("started_at")
    if (not code or not state or state != flow.get("state")
            or not isinstance(started_at, int) or isinstance(started_at, bool)
            or int(time.time()) - started_at > FLOW_MAX_AGE_SECONDS):
        return _fail(request, purpose, "state_mismatch")
    try:
        tokens = await microsoft_identity.exchange_code(
            config, code=code, code_verifier=str(flow.get("code_verifier", "")))
        identity = await microsoft_identity.validate_id_token(
            config, tokens.get("id_token"), nonce=str(flow.get("nonce", "")), now=int(time.time()))
    except InteractionRequired:
        return _fail(request, purpose, "microsoft_interaction_required")
    except (MicrosoftSignInError, IdTokenError) as exc:
        logger.warning("Microsoft sign-in failed: %s (tid=%s)", exc, getattr(exc, "tid", None))
        return _fail(request, purpose, "microsoft_failed")
    if purpose == "link":
        return await _finish_link(request, db, flow, identity)
    return await _finish_sign_in(request, db, identity)


async def _finish_sign_in(request: Request, db: AsyncSession,
                          identity: MicrosoftIdentity) -> RedirectResponse:
    user = await microsoft_identity.find_linked_user(db, identity)
    if user is None:
        logger.info("Microsoft sign-in refused: no linked account (tid=%s)", identity.tenant_id)
        return _fail(request, "signin", "microsoft_not_linked")
    if not may_use_microsoft_sign_in(user):
        logger.info("Microsoft sign-in refused: user %s has role %s (tid=%s)", user.id,
                    user.user_role, identity.tenant_id)
        return _fail(request, "signin", "microsoft_not_staff")
    if user.access_status == "allowed":  # a refused sign-in is not a use of the link
        await microsoft_identity.record_sign_in(db, user.id)
    return await complete_sign_in(request, db, user, via=VIA_MICROSOFT)


async def _finish_link(request: Request, db: AsyncSession, flow: dict,
                       identity: MicrosoftIdentity) -> RedirectResponse:
    """Re-check everything /auth/microsoft/link checked, against the session now."""
    current_user = await get_current_user(request, db)  # 302s a stale or revoked session
    refuse_impersonation(current_user, _IMPERSONATION_DETAIL)
    if (request.session.get(IMPERSONATE_KEY) or str(current_user.id) != flow.get("user_id")
            or not may_use_microsoft_sign_in(current_user)
            or not fresh_orcid_sign_in(request.session)):
        return _fail(request, "link", "microsoft_link_refused")
    user_id = current_user.id
    refusal = await microsoft_identity.link_account(db, user_id, identity)
    if refusal is not None:
        logger.info("Microsoft link refused for user %s: %s", user_id, refusal)
        return _fail(request, "link", refusal)
    await db.commit()
    logger.info("User %s linked a Microsoft sign-in (tid=%s)", user_id, identity.tenant_id)
    flash(request, "Microsoft sign-in linked. You can now use it on the sign-in page.", "success")
    return RedirectResponse(url="/settings", status_code=302)
