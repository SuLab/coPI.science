"""The open view (``LANDSCAPE_OPEN_VIEW``): a deployment with no sign-in at all, whose Landscape
results anyone who can reach the site may read.

Meant for a closed network (the Scripps test stack on an internal host), never production.
While the setting is on, ORCID and Microsoft sign-in are off (``src/routers/auth.py``,
``src/routers/microsoft_auth.py``), and a request with no signed-in session that GETs the
Landscape list or an opportunity's page is served as the *open viewer*: a manager-shaped
account that exists only in memory, so the pages show every current opportunity as staff see
them. Its capabilities are read-only (``WebCapabilities.read_only``): the templates drop every
form that writes, and the navigation shows the Landscape page alone. Any other path, and any
method but GET and HEAD, gets the ordinary answer for a visitor with no session, so nothing
can be written and no other page opens.
"""
import re
import uuid

from fastapi import Request

from src.config import get_settings
from src.models import USER_ROLE_MANAGER, User

#: The open viewer's id: in no ``users`` row, so every per-user lookup (own decisions,
#: assignments, expert samples) finds nothing.
OPEN_VIEWER_ID = uuid.UUID("00000000-0000-4000-8000-00000000f1e0")
OPEN_VIEWER_NAME = "Read-only visitor"
#: The pages the open viewer may GET: the Landscape list and an opportunity's page. Not the
#: pipeline page (``/workspace/landscape/pipeline``), which is an operator's.
_OPEN_PATH = re.compile(
    r"/workspace/landscape(?:/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})?"
)
#: Where the site root and the sign-in page send a visitor while the open view is on.
OPEN_LANDING = "/workspace/landscape?view=current&tab=all"


def open_view_enabled() -> bool:
    """Whether ``LANDSCAPE_OPEN_VIEW`` is on (read per request, so tests can flip it)."""
    return get_settings().landscape_open_view is True


def open_view_applies(request: Request) -> bool:
    """Whether this request is served as the open viewer: the setting is on, the session
    holds no signed-in user, the method is GET or HEAD, and the path is the Landscape list
    or an opportunity's page."""
    return (
        open_view_enabled()
        and not request.session.get("user_id")
        and request.method in ("GET", "HEAD")
        and _OPEN_PATH.fullmatch(request.url.path) is not None
    )


def open_viewer() -> User:
    """A fresh, never-persisted open viewer for one request (``is_open_viewer`` is True)."""
    viewer = User(
        id=OPEN_VIEWER_ID, name=OPEN_VIEWER_NAME, orcid="", user_role=USER_ROLE_MANAGER,
        access_status="allowed", onboarding_complete=True,
    )
    viewer._open_viewer = True  # type: ignore[attr-defined]
    return viewer


def is_open_viewer(user) -> bool:
    """Whether ``user`` is the open viewer ``open_viewer`` made."""
    return getattr(user, "_open_viewer", False) is True
