"""FastAPI application factory for CoPI/LabAgent."""

import logging
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import PlainTextResponse

from src.agent.ids import WRITER_WEB, set_default_writer_id
from src.config import Settings, get_settings
from src.routers import (
    admin,
    agent_page,
    assessment_chat,
    auth,
    csp_report,
    invite,
    manager,
    onboarding,
    profile,
    public,
    reviews,
)
from src.routers import settings as settings_router
from src.services.assessment_chat import drain_live_tasks
from src.web.errors import install_error_handlers
from src.web.security_headers import CSP_REPORT_PATH, SecurityHeadersMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


#: Methods that are not supposed to change state. Everything else must prove
#: it came from one of our own pages.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: The session cookie's name over plain HTTP (``ALLOW_HTTP_SESSIONS=true``: local
#: development and the browser harness).
SESSION_COOKIE_HTTP = "copi-session"
#: Its name when the cookie is Secure. The ``__Host-`` prefix makes a browser refuse
#: the cookie unless it is Secure, has ``Path=/`` and carries no ``Domain``, so a
#: sibling host on the shared registrable domain cannot set or overwrite it (A-05).
#: SessionMiddleware's defaults (path "/", no domain) satisfy all three.
SESSION_COOKIE_HTTPS = "__Host-copi-session"


def session_cookie_name(settings: Settings) -> str:
    """The session cookie name create_app() configures for ``settings``."""
    return SESSION_COOKIE_HTTP if settings.allow_http_sessions else SESSION_COOKIE_HTTPS

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
    session cookie would ride along. ``POST /profile/delete-account``
    (cascades nine tables) and, against a signed-in admin, ``POST
    /admin/users/{id}/role`` were both reachable that way (E1.1).

    Three signals, in strict precedence, all of them browser-set:

    1. a usable ``Origin`` (present and not the literal ``null``) must equal
       our own — and decides alone, so a mismatch is refused even when
       ``Sec-Fetch-Site`` says otherwise;
    2. failing that, ``Sec-Fetch-Site: same-origin`` — and ONLY
       ``same-origin`` (see ``SEC_FETCH_SAME_ORIGIN``);
    3. failing that, the origin component of ``Referer`` — but ONLY when there
       was no ``Origin`` header at all. An ``Origin: null`` has already had its
       one chance at step 2 and is refused here.

    Anything else is a 403.

    Added after SessionMiddleware in create_app(), because Starlette's
    ``add_middleware`` *prepends*: later added is further out. Only
    SecurityHeadersMiddleware sits outside it, and that layer refuses nothing.
    Outside the session is both correct and cheaper here — this reads headers
    only, so it refuses before the session is decoded or any route runs.

    One exemption, by exact path: ``POST CSP_REPORT_PATH`` (the CSP report sink),
    pinned by test_origin_guard.py::test_only_the_csp_report_path_is_exempt_from_the_origin_check.

    Not affected, verified rather than assumed: the ORCID callback is a GET;
    there is no inbound Slack POST route, and there is no CORSMiddleware.
    """

    async def dispatch(self, request: Request, call_next):
        if request.method.upper() in SAFE_METHODS:
            return await call_next(request)

        path = request.url.path

        if path == CSP_REPORT_PATH and request.method.upper() == "POST":
            # The CSP report sink (src/routers/csp_report.py): a browser's violation
            # report is not a form post from one of our pages and may carry no Origin
            # or an opaque one. Exact path and POST only; that route reads no session
            # and writes nothing but a bounded log line.
            return await call_next(request)

        expected = normalized_origin(get_settings().base_url)
        origin_raw = request.headers.get("origin")
        fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()

        # A literal "null" Origin is a real header carrying an OPAQUE origin,
        # not a missing header: sandboxed iframes, data: URLs and
        # `no-referrer` browsers all send it. It can never match, but it is
        # also not the same state as "the browser sent no Origin at all", and
        # the two are handled differently below.
        origin_is_opaque = origin_raw is not None and origin_raw.strip().lower() == "null"
        has_origin = origin_raw is not None and not origin_is_opaque

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
        elif fetch_site == SEC_FETCH_SAME_ORIGIN:
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
            allowed = True
        elif origin_is_opaque:
            # 3a. An OPAQUE origin stops here. Sec-Fetch-Site is its only
            #     rescue; it is deliberately NOT allowed to fall through to the
            #     Referer check below.
            #
            #     It costs no real user anything: the readers the branch above
            #     exists for are on a `no-referrer` policy and send no Referer
            #     at all, so a Referer fallback would never fire for them. And
            #     the one shape that WOULD produce "opaque origin plus a Referer
            #     on our own origin" is a sandboxed <iframe> pointed at one of
            #     our own pages — which today is stopped only by nginx's
            #     `X-Frame-Options: DENY`. This guard's correctness must not
            #     depend on a header set in another tier's config file.
            allowed = False
        else:
            # 3b. No Origin header at all: Referer's origin, for the browsers
            #     that send neither of the above.
            allowed = normalized_origin(request.headers.get("referer")) == expected

        if not allowed:
            logger.warning(
                "Refused cross-site %s %s (origin=%r, sec-fetch-site=%r, expected=%r)",
                request.method, path, origin_raw, fetch_site or None, expected,
            )
            return PlainTextResponse("Cross-site request refused.", status_code=403)

        return await call_next(request)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """No startup work of its own. On shutdown, give assessment-chat answers still
    streaming a chance to persist before the process exits (SB-11) — otherwise a
    deploy or a restart mid-answer leaves that turn `streaming` until
    `sweep_stale`'s STALE_AFTER_SECONDS window frees it. 8s is deliberately inside
    the web container's default 10s `docker stop` grace period (this compose
    service sets none of its own), so the process still exits on time either way;
    anything still running past it is exactly what the sweep exists for."""
    yield
    await drain_live_tasks(timeout=8.0)


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
        lifespan=_lifespan,
    )

    # One switch for the assessment chat, set once from the setting: the router
    # answers 503 when it is off and the shared detail template shows no button.
    # A state attribute rather than a Jinja global, because a global would have
    # to be registered in both routers' template setup.
    application.state.assessment_chat_enabled = settings.assessment_chat_enabled

    # Session middleware (signed cookies via itsdangerous)
    application.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=session_cookie_name(settings),
        max_age=30 * 24 * 3600,  # 30 days
        https_only=not settings.allow_http_sessions,
        same_site="lax",
    )

    # CSRF guard: the outermost middleware that can REFUSE a request (Starlette's
    # add_middleware prepends, so later calls wrap earlier ones). It reads headers
    # only and needs no session, so running it outside SessionMiddleware is both
    # correct and cheaper — a forged POST is refused before the session middleware
    # decodes a cookie.
    #
    # Outside the session is a REQUIREMENT, not a preference, and no request-level
    # assertion can see it (a refused request never modifies the session, so
    # SessionMiddleware emits no Set-Cookie either way). Pinned structurally by
    # test_origin_guard.py::test_the_guard_is_the_outermost_refusing_middleware.
    application.add_middleware(OriginGuardMiddleware)

    # Security headers and the per-request CSP nonce (spec §6.3). Added after the
    # guard, so it is the one layer OUTSIDE it: the guard's own 403 carries the
    # headers too, and every template sees request.state.csp_nonce. It refuses
    # nothing and reads no session or body, so the guard still refuses before any
    # session is decoded.
    application.add_middleware(SecurityHeadersMiddleware)

    # Static files. Served with `Cache-Control: no-cache` so a browser revalidates
    # against the ETag on every load and picks up a redeployed asset immediately.
    # Without this, /static carried only ETag/Last-Modified and no Cache-Control,
    # so browsers applied HEURISTIC freshness and kept serving a copy cached
    # before a deploy — which is why a fixed static/js/markdown.js did not reach
    # already-visited clients until a manual hard refresh.
    class _NoCacheStaticFiles(StaticFiles):
        async def get_response(self, path, scope):
            response = await super().get_response(path, scope)
            response.headers["Cache-Control"] = "no-cache"
            return response

    try:
        application.mount(
            "/static", _NoCacheStaticFiles(directory="static", html=True), name="static"
        )
    except RuntimeError:
        logger.warning("Static files directory not found, skipping mount")

    # Include routers
    application.include_router(public.router, tags=["public"])
    application.include_router(auth.router, tags=["auth"])
    application.include_router(csp_report.router, tags=["csp"])
    application.include_router(onboarding.router, prefix="/onboarding", tags=["onboarding"])
    application.include_router(profile.router, prefix="/profile", tags=["profile"])
    application.include_router(agent_page.router, prefix="/agent", tags=["agent"])
    application.include_router(admin.router, prefix="/admin", tags=["admin"])
    application.include_router(manager.router, prefix="/manager", tags=["manager"])
    application.include_router(reviews.router, prefix="/reviews", tags=["reviews"])
    application.include_router(
        assessment_chat.router, prefix="/assessment-chat", tags=["assessment-chat"]
    )
    application.include_router(invite.router, tags=["invite"])
    application.include_router(settings_router.router, prefix="/settings", tags=["settings"])
    # HTML error pages for browser navigation; JSON stays for scripts (src/web/errors.py).
    install_error_handlers(application)

    @application.get("/api/health")
    async def health():
        """Health check endpoint."""
        return {"status": "ok"}

    return application


app = create_app()
