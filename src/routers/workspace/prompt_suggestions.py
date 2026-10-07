"""Shared staff-only prompt-change triage, not assessment-chat questions."""

import hashlib
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_staff_user
from src.models import PromptChangeSuggestion, User
from src.services.assessment_reviews import (
    MAX_ANALYSES_PER_PRESS,
    count_pending_analysis_candidates,
)
from src.web.page_context import workspace_context
from src.web.templating import make_templates

router = APIRouter()
templates = make_templates()
_DB = Depends(get_db)
_STAFF = Depends(get_staff_user)
SUGGESTIONS_LIMIT = 200
_SUGGESTION_STATUSES = frozenset({"open", "dismissed", "implemented"})


def _prompt_file_status(entry: dict) -> dict:
    """Current-hash comparison for one recorded ``prompt_files`` entry.

    Computed here, at RENDER time, not stored: a suggestion's staleness is a
    fact about the *live* prompt set, and freezing it at write time would go
    stale itself the moment anything under ``prompts/`` changed.

    A stored ``sha256_12`` of ``None`` means the bot's own read failed when it
    ran (``review_bot._render_prompt_files``'s ``FileNotFoundError`` branch)
    — there is nothing to diff against, so that's reported as "missing" the
    same way a file that has since been deleted is, rather than invented as
    a false "stale".
    """
    path = entry.get("path", "")
    stored_hash = entry.get("sha256_12")
    try:
        current_hash = hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]
    except FileNotFoundError:
        current_hash = None
    if stored_hash is None or current_hash is None:
        badge = "missing"
    elif current_hash != stored_hash:
        badge = "stale"
    else:
        badge = None
    return {
        "path": path,
        "stored_hash": stored_hash,
        "current_hash": current_hash,
        "badge": badge,
    }


@router.get("/prompt-suggestions", response_class=HTMLResponse, name="workspace_prompt_suggestions")
async def workspace_prompt_suggestions(
    request: Request,
    status: str | None = None,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """The review bot's drafted prompt-edit queue. Read-only triage:
    the only writes this surface offers are the status action, which lives on
    ``POST /reviews/suggestions/{id}/status``, and the generate action, which
    lives on ``POST /reviews/suggestions/generate``. Both commands retain
    their authorization and impersonation checks on the review router."""
    status_filter = status if status in _SUGGESTION_STATUSES else None
    query = select(PromptChangeSuggestion)
    if status_filter:
        query = query.where(PromptChangeSuggestion.status == status_filter)
    total_count = (
        await db.execute(select(func.count()).select_from(query.subquery()))
    ).scalar_one()
    query = query.order_by(PromptChangeSuggestion.created_at.desc()).limit(SUGGESTIONS_LIMIT)
    suggestions = (await db.execute(query)).scalars().all()
    eligible_count = await count_pending_analysis_candidates(db)
    return templates.TemplateResponse(
        request,
        "workspace/prompt_suggestions.html",
        workspace_context(
            request,
            current_user,
            section="prompt-suggestions",
            suggestions=suggestions,
            status_filter=status_filter,
            total_count=total_count,
            suggestions_limit=SUGGESTIONS_LIMIT,
            eligible_count=eligible_count,
            analyses_per_press=MAX_ANALYSES_PER_PRESS,
        ),
    )


@router.get(
    "/prompt-suggestions/{suggestion_id}",
    response_class=HTMLResponse,
    name="workspace_prompt_suggestion_detail",
)
async def workspace_prompt_suggestion_detail(
    suggestion_id: uuid.UUID,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _STAFF,
):
    """One suggestion in full: the feedback it was distilled from, the
    interview/assessment it came from (if that row still exists —
    ``assessment_id`` is SET NULL, not CASCADE, on deletion), and per-file
    staleness against the prompt set on disk right now."""
    suggestion = (
        await db.execute(
            select(PromptChangeSuggestion).where(PromptChangeSuggestion.id == suggestion_id)
        )
    ).scalar_one_or_none()
    if suggestion is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    file_status = [_prompt_file_status(entry) for entry in suggestion.prompt_files]
    return templates.TemplateResponse(
        request,
        "workspace/prompt_suggestion_detail.html",
        workspace_context(
            request,
            current_user,
            section="prompt-suggestions",
            suggestion=suggestion,
            file_status=file_status,
        ),
    )
