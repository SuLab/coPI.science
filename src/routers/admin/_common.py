"""Names every admin submodule shares: the one ``router`` they all register on, the
template environment, and the dependency singletons."""

from fastapi import APIRouter, Depends, Request

from src.database import get_db
from src.dependencies import get_admin_user
from src.models import User
from src.web.templating import make_templates

router = APIRouter()
templates = make_templates()

# Valid AgentRegistry.status values (see src/models/agent_registry.py). Admins
# can move an already-approved agent between these from the edit page; the sim
# runs status=='active' agents (others are parked/excluded, reversibly).
VALID_AGENT_STATUSES = ("active", "inactive", "suspended", "pending")

# Module-level dependency singletons, the pattern src/routers/manager.py
# documents: ruff's B008 flags a `Depends(...)` call sitting in an argument
# default, and every such default counts against the src/ lint ratchet
# (scripts/ci.sh's SRC_LINT_MAX). New handlers here take these module-level
# singletons instead of adding to that debt.
_DB = Depends(get_db)
_ADMIN = Depends(get_admin_user)


def _template_context(
    request: Request, current_user: User, active_admin: str = "", **kwargs
) -> dict:
    """Build the template context, surfacing the impersonation banner.

    ``current_user`` here is the *effective* user from `get_admin_user`, which
    is the impersonated user when one admin impersonates another (both
    satisfy `is_admin`, so `get_admin_user` lets it through). Without this,
    every /admin/* page rendered with no banner and no Stop button. Mirrors
    the same pattern in onboarding.py / profile.py / agent_page.py /
    settings.py / manager.py.
    """
    impersonated = getattr(current_user, "_is_impersonated", False)
    real_admin = getattr(current_user, "_real_admin", None)
    ctx = {
        "request": request,
        "current_user": real_admin if impersonated else current_user,
        "impersonation_banner": current_user if impersonated else None,
        "active_page": "admin",
        "active_admin": active_admin,
    }
    ctx.update(kwargs)
    return ctx
