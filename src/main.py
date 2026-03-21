"""FastAPI application factory for CoPI/LabAgent."""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from src.config import get_settings
from src.routers import admin, auth, onboarding, profile

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()

    application = FastAPI(
        title="CoPI / LabAgent",
        description="Research collaboration platform with Slack-based AI agents",
        version="0.1.0",
    )

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
