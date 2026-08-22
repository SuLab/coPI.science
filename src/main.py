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


def normalized_origin(url: str | None) -> str | None:
    """``scheme://host[:port]`` for ``url``, or None if it carries no origin.

    Both sides of the comparison go through this. ``settings.base_url`` is
    configuration and may or may not have a trailing slash (production has
    none; other environments do), a browser's ``Origin`` header never has one,
    and a ``Referer`` is a full URL with a path — so raw string equality would
    be wrong in three different directions. ``Origin: null`` (what a sandboxed
    iframe sends) parses to no scheme and no netloc and therefore returns None,
    which never matches.
    """
    if not url:
        return None
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


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

        # Referer only when Origin is ABSENT — an Origin we cannot parse
        # ("null") is an answer, not a missing header, and must not fall
        # through to a Referer the same page also controls.
        sent_raw = request.headers.get("origin")
        if sent_raw is None:
            sent_raw = request.headers.get("referer")

        expected = normalized_origin(get_settings().base_url)
        sent = normalized_origin(sent_raw)
        if expected is None or sent is None or sent != expected:
            logger.warning(
                "Refused cross-site %s %s (origin=%r, expected=%r)",
                request.method, path, sent_raw, expected,
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
