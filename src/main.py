"""FastAPI application factory for CoPI/LabAgent."""

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

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
    application.include_router(manager.router, prefix="/manager", tags=["manager"])
    application.include_router(invite.router, tags=["invite"])
    application.include_router(settings_router.router, prefix="/settings", tags=["settings"])

    @application.get("/api/health")
    async def health():
        """Health check endpoint."""
        return {"status": "ok"}

    return application


app = create_app()
