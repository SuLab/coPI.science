"""Microsoft sign-in: start, link and the one shared callback (spec
docs/specs/2026-10-21-microsoft-sign-in-design.md §5.3-§5.6).

Every handler 404s while app.state.microsoft_sign_in is None (feature off). The callback is a
GET (response_mode=query): a form_post callback is a cross-site POST, which the SameSite=Lax
session cookie does not accompany. One session key, FLOW_KEY, carries a single attempt's state,
nonce, PKCE verifier and purpose; it is separate from ORCID's oauth_state, so neither provider's
callback can consume the other's flow.
"""

from __future__ import annotations

import logging
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import (
    account_of,
    get_current_user,
    impersonating,
    refuse_impersonation,
)
from src.models import User
from src.routers.auth import complete_sign_in
from src.services import microsoft_identity
from src.services.id_tokens import IdTokenError
from src.services.microsoft_identity import (
    InteractionRequired,
    MicrosoftIdentity,
    MicrosoftSignInConfig,
    MicrosoftSignInError,
)
from src.sign_in_session import VIA_MICROSOFT, fresh_orcid_sign_in

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


def _settings_redirect(code: str) -> RedirectResponse:
    return RedirectResponse(url=f"/settings?microsoft={code}", status_code=302)


def _fail(request: Request, purpose: object, code: str) -> RedirectResponse:
    """A linking or signed-in user lands on /settings?microsoft=<code>; anyone else on
    /login?error=<code>, because /login sends a signed-in user to / and would drop the code."""
    if purpose == "link" or request.session.get("user_id"):
        return _settings_redirect(code)
    return RedirectResponse(url=f"/login?error={code}", status_code=302)


def _is_impersonated(user: User) -> bool:
    return bool(getattr(user, "_is_impersonated", False))


def _start(
    request: Request, config: MicrosoftSignInConfig, *, purpose: str, user_id: str | None = None
) -> RedirectResponse:
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)  # 86 characters, within PKCE's 43-128
    request.session[FLOW_KEY] = {
        "state": state,
        "nonce": nonce,
        "code_verifier": code_verifier,
        "purpose": purpose,
        "user_id": user_id,
        "started_at": int(time.time()),
    }
    url = microsoft_identity.authorization_url(
        config,
        state=state,
        nonce=nonce,
        code_verifier=code_verifier,
        select_account=purpose == "link",
    )
    return RedirectResponse(url=url, status_code=302)


@router.get("/auth/microsoft/start")
async def microsoft_start(request: Request, config: MicrosoftSignInConfig = _CONFIG):
    """Begin a Microsoft sign-in from the /login button."""
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=302)
    # The redirect response carries the whole session cookie; a leftover from an abandoned
    # ORCID attempt would only add bytes to a header nginx buffers in 4 KB.
    request.session.pop("pending_access", None)
    request.session.pop("oauth_state", None)
    return _start(request, config, purpose="signin")


@router.get("/auth/microsoft/link")
async def microsoft_link_start(
    request: Request, config: MicrosoftSignInConfig = _CONFIG, current_user: User = _USER
):
    """Begin linking from Settings: not impersonating, and a proven ORCID re-sign-in at most
    15 minutes old (fresh_orcid_sign_in)."""
    if impersonating(request, account_of(current_user)):
        raise HTTPException(status_code=403, detail=_IMPERSONATION_DETAIL)
    refuse_impersonation(current_user, _IMPERSONATION_DETAIL)
    if not fresh_orcid_sign_in(request.session):
        return _settings_redirect("link_refused")
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
    # State first: an `error` is only believed, and logged, when it arrives on our own flow.
    if not isinstance(flow, dict) or not state or state != flow.get("state"):
        return _fail(request, None, "state_mismatch")
    purpose = flow.get("purpose")
    if error:
        # %r, not %s: both values come from the query string and may carry line breaks.
        logger.warning(
            "Microsoft sign-in error=%r description=%r",
            error[:80],
            request.query_params.get("error_description", "")[:300],
        )
        cancelled = error == "access_denied"
        return _fail(request, purpose, "microsoft_cancelled" if cancelled else "microsoft_error")
    started_at = flow.get("started_at")
    if (
        not code
        or not isinstance(started_at, int)
        or isinstance(started_at, bool)
        or int(time.time()) - started_at > FLOW_MAX_AGE_SECONDS
    ):
        return _fail(request, purpose, "state_mismatch")
    try:
        tokens = await microsoft_identity.exchange_code(
            config, code=code, code_verifier=str(flow.get("code_verifier", ""))
        )
        identity = await microsoft_identity.validate_id_token(
            config, tokens.get("id_token"), nonce=str(flow.get("nonce", "")), now=int(time.time())
        )
    except InteractionRequired:
        return _fail(request, purpose, "microsoft_interaction_required")
    except (MicrosoftSignInError, IdTokenError) as exc:
        logger.warning("Microsoft sign-in failed: %s (tid=%s)", exc, getattr(exc, "tid", None))
        return _fail(request, purpose, "microsoft_failed")
    if purpose == "link":
        return await _finish_link(request, db, flow, identity)
    return await _finish_sign_in(request, db, identity)


async def _finish_sign_in(
    request: Request, db: AsyncSession, identity: MicrosoftIdentity
) -> RedirectResponse:
    user = await microsoft_identity.find_linked_user(db, identity)
    if user is None:
        logger.info("Microsoft sign-in refused: no linked account (tid=%s)", identity.tenant_id)
        return _fail(request, "signin", "microsoft_not_linked")
    # A refused sign-in (not allowed) is not a use of the link. The stamp also re-checks the
    # link: one removed since find_linked_user read it must not sign anyone in.
    if user.access_status == "allowed" and not await microsoft_identity.record_sign_in(
        db, user.id, identity
    ):
        logger.info("Microsoft sign-in refused: user %s's link was removed during sign-in "
                    "(tid=%s)", user.id, identity.tenant_id)
        return _fail(request, "signin", "microsoft_not_linked")
    return await complete_sign_in(request, db, user, via=VIA_MICROSOFT)


async def _finish_link(
    request: Request, db: AsyncSession, flow: dict, identity: MicrosoftIdentity
) -> RedirectResponse:
    """Re-check everything /auth/microsoft/link checked, against the session now."""
    current_user = await get_current_user(request, db)  # 302s a stale or revoked session
    if impersonating(request, account_of(current_user)) or _is_impersonated(current_user):
        logger.info("Microsoft link refused for user %s: impersonating (tid=%s)",
                    account_of(current_user).id, identity.tenant_id)
        return _fail(request, "link", "impersonating")
    if str(current_user.id) != flow.get("user_id") or not fresh_orcid_sign_in(request.session):
        logger.info("Microsoft link refused for user %s: not the starting session or no fresh "
                    "ORCID re-sign-in (tid=%s)", current_user.id, identity.tenant_id)
        return _fail(request, "link", "link_refused")
    user_id = current_user.id
    refusal = await microsoft_identity.link_account(db, user_id, identity)
    if refusal is not None:
        logger.info("Microsoft link refused for user %s: %s (tid=%s)", user_id, refusal,
                    identity.tenant_id)
        return _fail(request, "link", refusal)
    await db.commit()
    logger.info("User %s linked a Microsoft sign-in (tid=%s)", user_id, identity.tenant_id)
    return _settings_redirect("linked")
