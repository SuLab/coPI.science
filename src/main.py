"""FastAPI application factory for CoPI/LabAgent."""

import logging
import uuid
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import PlainTextResponse

from src.agent.ids import WRITER_WEB, set_default_writer_id
from src.config import get_settings
from src.database import get_session_factory
from src.routers import admin, agent_page, auth, invite, manager, onboarding, profile, public
from src.routers import settings as settings_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


#: Methods that are not supposed to change state. Everything else must prove
#: it came from one of our own pages.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: The session cookie, spelled the same way create_app() configures it below.
SESSION_COOKIE = "copi-session"

#: Path prefixes whose POST is issued by a machine that cannot send an Origin.
#:
#: RFC 8058 one-click unsubscribe: src/services/email_notifications.py sets
#: ``List-Unsubscribe-Post: List-Unsubscribe=One-Click``, and the matching POST
#: to src/routers/settings.py's ``/settings/unsubscribe/{token}`` is issued
#: SERVER-SIDE by Gmail / Apple / Yahoo — no Origin, no Referer, no cookies.
#: Refusing it breaks one-click unsubscribe and with it bulk-sender compliance.
#: src/routers/auth.py already carries the same path as a non-browser exemption
#: (``_POST_LOGIN_DENY_PREFIXES``).
#:
#: The exemption is conditional on the request carrying NO session cookie (see
#: below), so it cannot be repurposed as a CSRF gadget: a real provider's
#: one-click POST has no cookies for us, and a forged one from a sibling tab
#: necessarily does.
ORIGINLESS_POST_PREFIXES = ("/settings/unsubscribe/",)

#: Ports a URL of that scheme omits by default. An origin does not include its
#: default port (RFC 6454 §4), so both sides are normalised against this.
DEFAULT_PORTS = {"http": 80, "https": 443}

#: The ONLY ``Sec-Fetch-Site`` value that counts as proof of same-origin.
#:
#: Never ``same-site``. That value is computed on the REGISTRABLE domain, so it
#: is precisely what ``copi.science`` and ``devel.copi.science`` would send
#: while attacking ``blackbird.copi.science`` — the exact case this guard
#: exists for. ``none`` (a typed URL or bookmark) and ``cross-site`` are
#: refused too.
SEC_FETCH_SAME_ORIGIN = "same-origin"


def normalized_origin(url: str | None) -> str | None:
    """``scheme://host[:port]`` for ``url``, or None if it carries no origin.

    Both sides of the comparison go through this, because raw string equality
    would be wrong in five different directions:

    * ``settings.base_url`` is configuration and may carry a trailing slash
      (production has none; other environments do);
    * a ``Referer`` is a full URL with a path and often a query string;
    * host comparison is case-insensitive, scheme comparison is not;
    * the DEFAULT PORT is not part of an origin (RFC 6454 §4 normalises it
      away), so ``https://host:443`` and ``https://host`` are the same origin.
      Browsers happen never to spell it out, but a reverse proxy, a redirect
      chain or a non-browser client will — comparing the strings is merely
      adequate for browsers rather than correct. A NON-default port stays
      significant, and so does the other scheme's default port
      (``https://host:80`` is not ``https://host``);
    * ``Origin: null`` — what a sandboxed iframe, a ``data:`` URL or a
      ``no-referrer`` browser sends — parses to no scheme and no netloc and
      therefore returns None, which never matches anything.
    """
    if not url:
        return None
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if not scheme or not parts.netloc:
        return None
    try:
        port = parts.port
    except ValueError:
        # A non-numeric port is not something we can reason about; refusing to
        # produce an origin makes it fail closed rather than compare equal.
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    if ":" in host:  # IPv6 literal — urlsplit strips the brackets, put them back
        host = f"[{host}]"
    if port is None or port == DEFAULT_PORTS.get(scheme):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


class OriginGuardMiddleware(BaseHTTPMiddleware):
    """Refuse state-changing requests that did not come from our own origin.

    There was no request-side CSRF check anywhere in src/, and the only defence
    was ``same_site="lax"`` on the session cookie. That defence is void in this
    deployment: one nginx serves ``blackbird.copi.science``, ``copi.science``
    (an unrelated production tenant) and ``devel.copi.science``. SameSite is
    computed on the REGISTRABLE domain, so all three count as the same site — a
    page on either sibling could auto-submit a top-level POST and the victim's
    ``copi-session`` cookie would ride along. ``POST /profile/delete-account``
    (cascades nine tables) and, against a signed-in admin, ``POST
    /admin/users/{id}/role`` were both reachable that way (E1.1).

    Three signals, in strict precedence, all of them browser-set:

    1. a usable ``Origin`` (present and not the literal ``null``) must equal
       our own — and decides alone, so a mismatch is refused even when
       ``Sec-Fetch-Site`` says otherwise;
    2. failing that, ``Sec-Fetch-Site: same-origin`` — and ONLY
       ``same-origin`` (see ``SEC_FETCH_SAME_ORIGIN``);
    3. failing that, the origin component of ``Referer``.

    Anything else is a 403.

    Added LAST in create_app(), because Starlette's ``add_middleware``
    *prepends*: last added is outermost. Outermost is both correct and cheaper
    here — this reads headers only and needs no session, so it refuses before
    AgentBadgeMiddleware opens a connection and runs its per-agent COUNTs.

    Not affected, verified rather than assumed: the ORCID callback is a GET;
    there is no inbound Slack POST route; ``POST /api/proposal-vote`` takes a
    JSON body and there is no CORSMiddleware, so it was already preflight-bound.
    """

    async def dispatch(self, request: Request, call_next):
        if request.method.upper() in SAFE_METHODS:
            return await call_next(request)

        path = request.url.path
        if path.startswith(ORIGINLESS_POST_PREFIXES) and SESSION_COOKIE not in request.cookies:
            return await call_next(request)

        expected = normalized_origin(get_settings().base_url)
        origin_raw = request.headers.get("origin")
        fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()

        # A literal "null" Origin is a real header with an opaque value, not a
        # missing one: sandboxed iframes, data: URLs and `no-referrer` browsers
        # all send it. It can never match, so it is treated as "no usable
        # Origin" and the weaker signals below get their turn.
        has_origin = origin_raw is not None and origin_raw.strip().lower() != "null"

        if expected is None:
            # Misconfigured base_url. Fail closed rather than compare equal to
            # everything.
            allowed = False
        elif has_origin:
            # 1. A usable Origin decides ON ITS OWN, in both directions. A
            #    mismatch is refused even when Sec-Fetch-Site claims
            #    same-origin, so the two signals can never be played against
            #    each other — defence in depth, and it costs nothing.
            allowed = normalized_origin(origin_raw) == expected
        else:
            # 2. No usable Origin. Sec-Fetch-Site is the browser's own answer
            #    to the question this guard is asking, and it is the ONLY thing
            #    that keeps the site usable for a reader whose browser (or
            #    extension, or enterprise policy) sends `no-referrer`: those
            #    send Origin: null AND no Referer on a SAME-ORIGIN form POST,
            #    which without this branch 403s every form on the site.
            #
            #    This is not a weakening. Sec-Fetch-* are FORBIDDEN HEADER
            #    NAMES: page script cannot set them, and a browser will not let
            #    an attacker's page forge one. Only a non-browser client can —
            #    and a non-browser client carries no ambient session cookie, so
            #    it is not a CSRF vector in the first place.
            #
            # 3. Referer's origin, last, for the browsers that send neither.
            allowed = (
                fetch_site == SEC_FETCH_SAME_ORIGIN
                or normalized_origin(request.headers.get("referer")) == expected
            )

        if not allowed:
            logger.warning(
                "Refused cross-site %s %s (origin=%r, sec-fetch-site=%r, expected=%r)",
                request.method, path, origin_raw, fetch_site or None, expected,
            )
            return PlainTextResponse("Cross-site request refused.", status_code=403)

        return await call_next(request)


class AgentBadgeMiddleware(BaseHTTPMiddleware):
    """Inject unreviewed proposal count into request.state for nav badge."""

    async def dispatch(self, request: Request, call_next):
        # Asset and health probes carry no nav and need no badge; without this
        # guard every /static request with a session cookie ran the per-agent
        # COUNT queries below (issue #25 P1 — nginx has no location /static
        # block, so they all reach uvicorn).
        path = request.url.path
        if path.startswith("/static/") or path == "/api/health":
            return await call_next(request)
        request.state.posthog_api_key = get_settings().posthog_api_key
        request.state.agent_badge_count = 0
        user_id_str = request.session.get("user_id") if "session" in request.scope else None
        if user_id_str:
            try:
                from src.models import (
                    AgentDelegate,
                    AgentRegistry,
                    ProposalReview,
                    ThreadDecision,
                    User,
                )
                session_factory = get_session_factory()
                async with session_factory() as db:
                    uid = uuid.UUID(user_id_str)

                    # Honor the impersonate cookie only for admins — it is an
                    # unsigned client cookie, so without this gate any logged-in
                    # user could read another user's badge count (SEC-12). This
                    # mirrors the is_admin check in get_current_user. The extra
                    # query runs only when the cookie is actually present.
                    impersonate_id = request.cookies.get("copi-impersonate")
                    if impersonate_id:
                        is_admin = await db.scalar(
                            select(User.is_admin).where(User.id == uid)
                        )
                        if is_admin:
                            try:
                                uid = uuid.UUID(impersonate_id)
                            except ValueError:
                                pass

                    # Get all agent_ids the user has access to (own + delegated)
                    own_result = await db.execute(
                        select(AgentRegistry.agent_id).where(
                            AgentRegistry.user_id == uid,
                            AgentRegistry.status == "active",
                        )
                    )
                    delegated_result = await db.execute(
                        select(AgentRegistry.agent_id)
                        .join(AgentDelegate, AgentDelegate.agent_registry_id == AgentRegistry.id)
                        .where(
                            AgentDelegate.user_id == uid,
                            AgentRegistry.status == "active",
                        )
                    )
                    agent_ids = [r[0] for r in own_result] + [r[0] for r in delegated_result]

                    if agent_ids:
                        badge_count = 0
                        for aid in agent_ids:
                            total_result = await db.execute(
                                select(func.count(ThreadDecision.id)).where(
                                    ThreadDecision.outcome == "proposal",
                                    (ThreadDecision.agent_a == aid) | (ThreadDecision.agent_b == aid),
                                )
                            )
                            total = total_result.scalar() or 0
                            reviewed_result = await db.execute(
                                select(func.count(ProposalReview.id)).where(
                                    ProposalReview.agent_id == aid
                                )
                            )
                            reviewed = reviewed_result.scalar() or 0
                            badge_count += max(0, total - reviewed)
                        request.state.agent_badge_count = badge_count
            except Exception as exc:
                # Deliberately swallowed: this middleware only computes a nav
                # badge count, and no page should 500 because a count failed.
                # But it is LOGGED — the last bare `except Exception: pass` in
                # src/ hid a dead import in invite.py for an unknown length of
                # time (the delegate Slack sync never ran once), so a silent
                # swallow here would hide a broken query just as well.
                logger.warning("Badge-count middleware failed, continuing: %s", exc)
        return await call_next(request)


def create_app() -> FastAPI:
    settings = get_settings()

    # Claim the web process's canonical-id writer slot, so PI messages and DMs
    # written here can never collide with ids minted by the engine or any other
    # writer process (R1). See src/agent/ids.py.
    set_default_writer_id(WRITER_WEB)

    application = FastAPI(
        title="CoPI / LabAgent",
        description="Research collaboration platform with Slack-based AI agents",
        version="0.1.0",
        # No public API documentation. FastAPI mounts /docs, /docs/oauth2-redirect,
        # /redoc and /openapi.json by default and puts NONE of them behind auth, so
        # they published the whole route inventory — every path, method and form
        # field name — to anonymous callers. That is the reconnaissance half of the
        # CSRF problem OriginGuardMiddleware (this module) closes (E1.4). Passing
        # None unregisters the routes outright, so they 404 rather than 401.
        # `application.openapi()` still builds the schema in-process, which is what
        # tests/unit/test_reachability.py's route walk needs.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Agent badge middleware (added first so it runs inside session middleware)
    application.add_middleware(AgentBadgeMiddleware)

    # Session middleware (signed cookies via itsdangerous)
    application.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=SESSION_COOKIE,
        max_age=30 * 24 * 3600,  # 30 days
        https_only=not settings.allow_http_sessions,
        same_site="lax",
    )

    # CSRF guard. Added LAST, so it is the OUTERMOST middleware: Starlette's
    # add_middleware prepends. It reads headers only and needs no session, so
    # running it outside SessionMiddleware is both correct and cheaper — a
    # forged POST is refused before AgentBadgeMiddleware opens a connection.
    #
    # Outermost is a REQUIREMENT, not a preference, and no request-level
    # assertion can see it (a refused request never modifies the session, so
    # SessionMiddleware emits no Set-Cookie either way). It is pinned
    # structurally by test_origin_guard.py::test_the_guard_is_the_outermost_middleware.
    application.add_middleware(OriginGuardMiddleware)

    # Static files
    try:
        application.mount("/static", StaticFiles(directory="static", html=True), name="static")
    except RuntimeError:
        logger.warning("Static files directory not found, skipping mount")

    # Include routers
    application.include_router(public.router, tags=["public"])
    application.include_router(auth.router, tags=["auth"])
    application.include_router(onboarding.router, prefix="/onboarding", tags=["onboarding"])
    application.include_router(profile.router, prefix="/profile", tags=["profile"])
    application.include_router(agent_page.router, prefix="/agent", tags=["agent"])
    application.include_router(admin.router, prefix="/admin", tags=["admin"])
    application.include_router(manager.router, prefix="/manager", tags=["manager"])
    application.include_router(invite.router, tags=["invite"])
    application.include_router(settings_router.router, prefix="/settings", tags=["settings"])

    @application.get("/api/health")
    async def health():
        """Health check endpoint."""
        return {"status": "ok"}

    return application


app = create_app()
