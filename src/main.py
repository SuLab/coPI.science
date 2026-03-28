"""FastAPI application factory for CoPI/LabAgent."""

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from src.config import get_settings
from src.database import get_session_factory
from src.routers import admin, agent_page, auth, onboarding, profile

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
                from src.models import AgentRegistry, ProposalReview, ThreadDecision
                session_factory = get_session_factory()
                async with session_factory() as db:
                    # Get agent for this user
                    result = await db.execute(
                        select(AgentRegistry.agent_id).where(
                            AgentRegistry.user_id == uuid.UUID(effective_user_id),
                            AgentRegistry.status == "active",
                        )
                    )
                    row = result.first()
                    if row:
                        aid = row[0]
                        # Count proposals not yet reviewed by this agent
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
                        request.state.agent_badge_count = max(0, total - reviewed)
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
    application.include_router(auth.router, tags=["auth"])
    application.include_router(onboarding.router, prefix="/onboarding", tags=["onboarding"])
    application.include_router(profile.router, prefix="/profile", tags=["profile"])
    application.include_router(agent_page.router, prefix="/agent", tags=["agent"])
    application.include_router(admin.router, prefix="/admin", tags=["admin"])

    @application.get("/")
    async def root(request: Request):
        """Root redirect — logged-in users go to profile, others to login."""
        if request.session.get("user_id"):
            return RedirectResponse(url="/profile", status_code=302)
        return RedirectResponse(url="/login", status_code=302)

    @application.get("/api/health")
    async def health():
        """Health check endpoint."""
        return {"status": "ok"}

    return application


app = create_app()
