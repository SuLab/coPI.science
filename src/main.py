"""FastAPI application factory for CoPI/LabAgent."""

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from src.config import get_settings
from src.database import get_session_factory
from src.routers import admin, agent_page, auth, invite, onboarding, profile, public
from src.routers import settings as settings_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class AgentBadgeMiddleware(BaseHTTPMiddleware):
    """Inject unreviewed proposal count into request.state for nav badge."""

    async def dispatch(self, request: Request, call_next):
        request.state.agent_badge_count = 0
        user_id_str = request.session.get("user_id") if "session" in request.scope else None
        # Use impersonated user if applicable
        impersonate_id = request.cookies.get("copi-impersonate") if user_id_str else None
        effective_user_id = impersonate_id or user_id_str
        if effective_user_id:
            try:
                from src.models import AgentDelegate, AgentRegistry, ProposalReview, ThreadDecision
                session_factory = get_session_factory()
                async with session_factory() as db:
                    uid = uuid.UUID(effective_user_id)

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
            except Exception:
                pass
        return await call_next(request)


def create_app() -> FastAPI:
    settings = get_settings()

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
        application.mount("/static", StaticFiles(directory="static"), name="static")
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
        """Health check endpoint."""
        return {"status": "ok"}

    return application


app = create_app()
