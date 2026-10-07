"""Names every admin submodule shares: the one ``router`` they all register on, the
template environment, and the dependency singletons."""

from fastapi import APIRouter, Depends, Request

from src.database import get_db
from src.dependencies import get_admin_user
from src.models import User
from src.web.templating import make_templates
from src.web.page_context import page_context

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


def _template_context(request: Request, current_user: User, active_admin: str = "", **kwargs) -> dict:
    return page_context(request, current_user, active_page="admin", active_admin=active_admin, **kwargs)
