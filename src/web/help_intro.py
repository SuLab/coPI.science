"""The first-sign-in prompt's state for workspace pages (spec
docs/specs/2026-10-14-help-and-onboarding-design.md §5.2).

A workspace-router dependency: it raises no HTTPException of its own (router dependencies
run before a route's own gate, so a raising check here would change a staff route's
refusal), and database errors propagate (a swallowed error would leave the shared session
in a failed transaction). HEAD reaches it as GET (src/web/head_requests.py).
"""

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_current_user
from src.models.help import HelpIntroDismissal
from src.web.help_pages import tutorial_key_for

# Module-level Depends, as src/routers/auth.py does: a call in a default argument is a
# ruff B008 finding, and src/ is under a ratcheted lint ceiling.
_USER = Depends(get_current_user)
_DB = Depends(get_db)


async def load_help_intro(request: Request, user=_USER, db: AsyncSession = _DB) -> None:
    """Set ``request.state.help_intro`` to the role's tutorial key when the prompt is owed:
    a GET by a manager or reviewer, not impersonated, with no answer recorded."""
    if request.method != "GET" or getattr(user, "_is_impersonated", False):
        return
    key = tutorial_key_for(user.user_role)
    if key is None:
        return
    answered = await db.scalar(
        select(HelpIntroDismissal.user_id).where(HelpIntroDismissal.user_id == user.id))
    if answered is None:
        request.state.help_intro = key
