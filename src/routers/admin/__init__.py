"""Admin routes (spec §7.3 split of the former src/routers/admin.py). ``router`` is
the one APIRouter every submodule registers on (defined in ``_common``); src/main.py
mounts it under /admin. Importing the submodules below, in the order the monolith
registered their first routes, is what populates it. A single shared router rather
than ``include_router`` per module: ``admin_users`` is registered at ``""`` and
FastAPI refuses to include a router whose route has an empty path and no prefix."""

# isort: off
# Route registration order is import order; keep the monolith's.
from src.routers.admin import users  # noqa: E402,F401
from src.routers.admin import jobs  # noqa: E402,F401
from src.routers.admin import runs  # noqa: E402,F401
from src.routers.admin import discussions  # noqa: E402,F401
from src.routers.admin import agents  # noqa: E402,F401
from src.routers.admin import assessments  # noqa: E402,F401
from src.routers.admin import impersonation  # noqa: E402,F401
from src.routers.admin import access  # noqa: E402,F401
from src.routers.admin import cohorts  # noqa: E402,F401
from src.routers.admin import simulation  # noqa: E402,F401
# isort: on
from src.routers.admin._common import router

__all__ = [
    "access", "agents", "assessments", "cohorts", "discussions", "impersonation",
    "jobs", "router", "runs", "simulation", "users",
]
