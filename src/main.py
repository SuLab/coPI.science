"""FastAPI application factory for CoPI/LabAgent."""

import asyncio
import json
import logging
import uuid

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from src.agent.ids import WRITER_WEB, set_default_writer_id
from src.config import get_settings
from src.database import get_session_factory
from src.routers import admin, agent_page, auth, invite, onboarding, profile, public
from src.routers import settings as settings_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Cap on a CSP violation report's body (#27 I5 RC-5): /api/csp-report is public and
# unauthenticated, so an unbounded body is a way to push arbitrary bytes into the app
# logs. Real reports (either format) are well under 1 KB.
CSP_REPORT_MAX_BODY_BYTES = 8 * 1024

# The only content types a real CSP violation report arrives with: the legacy
# `application/csp-report` (older Chrome/Firefox), the newer Reporting API's
# `application/reports+json`, and `application/json` (some browsers/versions send
# plain JSON). Anything else is refused with 415 before the body is even parsed
# (opus review, audit 2026-09-08 RC-5: this was documented but never enforced).
CSP_REPORT_ALLOWED_CONTENT_TYPES = frozenset(
    {"application/csp-report", "application/reports+json", "application/json"}
)

# Cap on one logged CSP-report field (opus review, audit 2026-09-08 RC-5): every field
# below comes straight from an unauthenticated, attacker-controlled request body.
# Truncating AND stripping \n/\r before it reaches the logger closes CRLF log
# injection (a crafted document-uri could otherwise forge additional log lines).
CSP_REPORT_LOG_FIELD_MAX_CHARS = 200


def _sanitize_csp_log_field(value: object) -> str:
    # SEC2-3 (audit 2026-09-08): \n/\r were not the only way to forge a fake
    # log line or corrupt a terminal/log viewer — ANSI escapes (\x1b[...) and
    # Unicode line/paragraph separators (U+2028/U+2029) are non-printable
    # too. Strip every non-printable character rather than special-casing
    # CR/LF.
    text = str(value)[:CSP_REPORT_LOG_FIELD_MAX_CHARS]
    return "".join(c if c.isprintable() else " " for c in text)

# Bounds the /api/health DB probe (#27 I2 review): without this, a stalled
# Postgres (TCP open, no query response) piles orphaned probe coroutines and
# connections against the pool instead of failing fast.
HEALTH_PROBE_TIMEOUT_SECONDS = 5.0

# `asyncio.wait_for` alone does NOT deliver that bound. SQLAlchemy's asyncpg adapter
# runs the query inside a greenlet, and cancelling the awaiting task cannot interrupt a
# socket read the greenlet is already parked on. Measured against a FROZEN Postgres
# (`docker pause`, i.e. the "TCP open, no response" case the comment above names):
# wait_for returned only after 142s — when the server was thawed — while the same probe
# with asyncpg's own `command_timeout` armed returned in 5.00s. asyncpg's timer lives
# inside the driver and tears the transport down, which is what lets the cancellation
# land. So the probe gets its own engine: `command_timeout` set just under the outer
# bound, and its own one-connection pool (see get_health_engine) so a hung probe cannot
# consume (or leak) a connection from the pool the application serves requests from.
HEALTH_PROBE_COMMAND_TIMEOUT_SECONDS = 4.0
# ...and `command_timeout` alone is not enough either, because the probe still has to
# *open* connections — the first one, and every one that replaces a reaped connection:
# against a frozen server the TCP handshake completes into the kernel's accept backlog
# and then the startup/authentication exchange hangs, which is asyncpg's CONNECT
# timeout, not its command timeout. Measured (on the NullPool probe this started as,
# which opened one per request): with only command_timeout set, three consecutive probes
# each ran past 30s. With both, the probe fails fast. Keep both under
# HEALTH_PROBE_TIMEOUT_SECONDS so the driver, not the outer wait_for, gives up first.
# (Against a *frozen* server — `docker pause`, as opposed to a slow one — it is in fact
# the outer wait_for that delivers, measured at exactly 5.00s: asyncpg's command timer
# fires at 4.0s but its cancel handshake needs a second connection to the same frozen
# server. That is why nothing may be awaited outside that outer bound.)
HEALTH_PROBE_CONNECT_TIMEOUT_SECONDS = 3.0
_health_engine = None


def get_health_engine():
    """Lazily-built one-connection engine used only by /api/health. See the note above."""
    global _health_engine
    if _health_engine is None:
        from sqlalchemy.ext.asyncio import create_async_engine

        from src.config import get_settings

        # A tiny dedicated pool, not NullPool. NullPool opens a fresh connection per
        # probe, and /api/health is publicly reachable — one request to one Postgres
        # connection is an amplifier a load generator can point at the database
        # (over-implementation audit). pool_size=1/max_overflow=0 keeps the probe off
        # the request pool (its whole purpose) while capping it at a single connection;
        # concurrent probes queue behind it, and pool_timeout keeps that queue bounded
        # well inside HEALTH_PROBE_TIMEOUT_SECONDS.
        _health_engine = create_async_engine(
            get_settings().database_url,
            pool_size=1,
            max_overflow=0,
            pool_timeout=HEALTH_PROBE_CONNECT_TIMEOUT_SECONDS,
            # Deliberately NOT pool_pre_ping, and this is measured, not assumed.
            # The pooled connection really does go stale — a Postgres restart or an
            # idle reaper leaves a dead socket in the pool and the next probe reports
            # a healthy database unavailable — but SQLAlchemy runs the pre-ping inside
            # engine.connect(), which is OUTSIDE the probe's asyncio bound below, and
            # against a FROZEN server (`docker pause`) asyncpg's own command_timeout
            # does not rescue it: the driver's cancel handshake needs a second
            # connection to the same frozen server. Measured in one pause window
            # against the disposable production copy: this engine answered 503 in
            # 5.00s, the same engine with pool_pre_ping=True never answered at all
            # (>15s and >25s in two runs) — i.e. pre-ping reopens phase-8 audit C2,
            # the Critical this probe's timeouts exist to close. The stale connection
            # is handled where it can be bounded instead: see the retry in /api/health.
            pool_pre_ping=False,
            connect_args={
                "command_timeout": HEALTH_PROBE_COMMAND_TIMEOUT_SECONDS,
                "timeout": HEALTH_PROBE_CONNECT_TIMEOUT_SECONDS,
            },
        )
    return _health_engine


class AgentBadgeMiddleware(BaseHTTPMiddleware):
    """Inject unreviewed proposal count into request.state for nav badge."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in ("/api/health", "/api/csp-report") or path.startswith("/static/"):
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
                                    # Scope the reviewed count to the SAME rows the total
                                    # counts: proposals this agent participated in. Counting
                                    # every ProposalReview bearing this agent_id also counted
                                    # reviews of decisions whose outcome later moved off
                                    # 'proposal', so `total - reviewed` went negative for 22 of
                                    # 53 active agents on production data and the badge silently
                                    # clamped real outstanding work to 0 (issue #20 closure audit;
                                    # measured 166 outstanding roster-wide vs 98 reported).
                                select(func.count(ProposalReview.id))
                                .join(
                                    ThreadDecision,
                                    ThreadDecision.id == ProposalReview.thread_decision_id,
                                )
                                .where(
                                    ProposalReview.agent_id == aid,
                                    # Both sentinels, not just -1: neither is
                                    # submittable (agent_page.py:509,
                                    # email_inbound.py:383), so counting 0 as a review
                                    # hid outstanding work from the badge for 12 of 53
                                    # active agents on the production copy (wiseman by
                                    # 89). A reopened proposal IS outstanding -- the PI's
                                    # attention is needed for the refinement, and their
                                    # rating arrives by e-mail reply, not the web form
                                    # (see task-D1-D2.md).
                                    ProposalReview.rating.notin_((-1, 0)),
                                    ThreadDecision.outcome == "proposal",
                                    (ThreadDecision.agent_a == aid)
                                    | (ThreadDecision.agent_b == aid),
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
    # written here can never collide with ids minted by the engine or GrantBot
    # processes (R1). See src/agent/ids.py.
    set_default_writer_id(WRITER_WEB)

    application = FastAPI(
        title="CoPI / LabAgent",
        description="Research collaboration platform with Slack-based AI agents",
        version="0.1.0",
    )

    # Agent badge middleware (added first so it runs inside session middleware)
    application.add_middleware(AgentBadgeMiddleware)

    # Session middleware (signed cookies via itsdangerous)
    application.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="copi-session",
        max_age=30 * 24 * 3600,  # 30 days
        https_only=not settings.allow_http_sessions,
        same_site="lax",
    )

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
    application.include_router(invite.router, tags=["invite"])
    application.include_router(settings_router.router, prefix="/settings", tags=["settings"])

    @application.get("/api/health")
    async def health():
        """Health check endpoint. Probes the DB so a broken schema or a
        downed Postgres is never reported healthy (#27 I2 — on 2026-07-30
        this route returned 200 while every ORM read of agent_messages
        raised UndefinedColumnError, and nginx's depends_on: service_healthy
        let traffic through regardless)."""

        async def probe_once() -> None:
            # `engine.connect()` is inside this coroutine (not wrapped in its own
            # wait_for) so the caller can bound BOTH connect and execute under one
            # deadline — a probe that only bounded conn.execute() left connect() (and,
            # on retry, a second full connect+execute) free to run past
            # HEALTH_PROBE_TIMEOUT_SECONDS (#27 I2 audit RC-10).
            async with get_health_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))

        loop = asyncio.get_running_loop()
        deadline = loop.time() + HEALTH_PROBE_TIMEOUT_SECONDS

        try:
            try:
                await asyncio.wait_for(probe_once(), timeout=HEALTH_PROBE_TIMEOUT_SECONDS)
            except DBAPIError as exc:
                # The probe pools one connection, so between probes it sits idle and a
                # Postgres restart, an idle-connection reaper or a pg_terminate_backend
                # can reap it. Its next use fails on a socket the database already
                # closed, which is not the database being unavailable. SQLAlchemy has
                # already invalidated and discarded that connection by the time this
                # runs (connection_invalidated), so one retry gets a fresh one — but
                # only with whatever remains of the outer deadline, never a fresh full
                # budget, so the response still lands inside HEALTH_PROBE_TIMEOUT_SECONDS
                # even counting the first attempt's connect+execute time.
                if not exc.connection_invalidated:
                    raise
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise
                logger.info("Health probe reconnecting after a reaped connection: %s", exc)
                await asyncio.wait_for(probe_once(), timeout=remaining)
        except Exception as exc:
            logger.warning("Health check DB probe failed: %s", exc)
            raise HTTPException(status_code=503, detail="database unavailable") from exc
        return {"status": "ok"}

    @application.post("/api/csp-report", include_in_schema=False)
    async def csp_report(request: Request) -> Response:
        """Collects violation reports for the Report-Only CSP header nginx sends
        (`report-uri /api/csp-report`; #27 I5, audit RC-5 — the header previously had
        no report-uri/report-to at all, so nothing was enforced OR collected). Public
        and unauthenticated: the browser sending a report never carries this app's
        session cookie. Accepts only the content types browsers actually send for a
        report (`CSP_REPORT_ALLOWED_CONTENT_TYPES`) — anything else is refused with
        415 before the body is read; a malformed or oversized body is refused or
        dropped, never a 500."""
        content_type = request.headers.get("content-type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type not in CSP_REPORT_ALLOWED_CONTENT_TYPES:
            raise HTTPException(status_code=415, detail="unsupported content type")

        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > CSP_REPORT_MAX_BODY_BYTES:
                    raise HTTPException(status_code=413, detail="report too large")
            except ValueError:
                pass  # malformed header; the actual-length check below still applies
        body = await request.body()
        if len(body) > CSP_REPORT_MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="report too large")

        document_uri = violated_directive = blocked_uri = "unknown"
        try:
            payload = json.loads(body)
            if isinstance(payload, dict):
                # application/csp-report: {"csp-report": {"document-uri": ..., ...}}
                report = payload.get("csp-report") or {}
                document_uri = report.get("document-uri", document_uri)
                violated_directive = report.get("violated-directive", violated_directive)
                blocked_uri = report.get("blocked-uri", blocked_uri)
            elif isinstance(payload, list) and payload:
                # application/reports+json: [{"body": {"documentURL": ..., ...}}, ...]
                report = payload[0].get("body") or {}
                document_uri = report.get("documentURL", document_uri)
                violated_directive = report.get("effectiveDirective", violated_directive)
                blocked_uri = report.get("blockedURL", blocked_uri)
        except (json.JSONDecodeError, AttributeError, TypeError, IndexError) as exc:
            logger.warning("CSP violation report was not parseable: %s", exc)
            return Response(status_code=204)

        logger.warning(
            "CSP violation: document-uri=%s violated-directive=%s blocked-uri=%s",
            _sanitize_csp_log_field(document_uri),
            _sanitize_csp_log_field(violated_directive),
            _sanitize_csp_log_field(blocked_uri),
        )
        return Response(status_code=204)

    return application


app = create_app()
