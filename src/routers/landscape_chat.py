"""The landscaping chat's routes (docs/specs/2026-10-30-blackbird-landscaping-design.md §13.5):
the assessment chat's drawer, asked about one landscaping opportunity's current assessment.

Mounted at /landscape-chat for admin, manager and reviewer alike, copied from
src/routers/project_chat.py, whose module docstring holds for every route here. Every route,
in order: 403 while impersonating, 403 for a signed-in user who is neither staff nor
reviewer, 503 unless both the assessment chat's and the landscaping chat's switches are on,
404 for a malformed or unknown opportunity id or one the viewer may not see
(``landscape_view.can_view``: a reviewer reads only opportunities in issued batches or in
their own expert sample, D50, D63), and for every POST 415 unless the body is JSON. Asking
also answers 503 ``prompt_unrecorded`` while ``prompts/landscaping/chat.md`` does not match
its recorded hash (spec §8.1). Every read and write is keyed on (opportunity, signed-in
user, tier), and the conversation spans the opportunity's assessment versions (X7); every
response carries `Cache-Control: no-store`; every handler catches SQLAlchemyError at its
boundary and logs only the class and the ids.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_current_user
from src.landscaping import prompt_set
from src.models import User
from src.models.landscape import LandscapeOpportunity
from src.routers.assessment_chat import _NO_STORE, _STREAM_HEADERS, _body, _error, _is_json
from src.services import assessment_chat as chat
from src.services import landscape_view
from src.services.web_permissions import can_review

logger = logging.getLogger(__name__)
router = APIRouter()

_DB = Depends(get_db)
_USER = Depends(get_current_user)


def _refused(request: Request, current_user: User) -> JSONResponse | None:
    # The attribute exists only on an impersonated user object (src/dependencies.py).
    if getattr(current_user, "_is_impersonated", False):
        return _error(403, "impersonating")
    if not can_review(current_user):
        return _error(403, "forbidden")
    state = request.app.state
    if not (
        getattr(state, "assessment_chat_enabled", False)
        and getattr(state, "landscape_chat_enabled", False)
    ):
        return _error(503, "disabled")
    return None


def _parse_opportunity_id(opportunity_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(opportunity_id)
    except ValueError:
        return None


async def _unavailable(
    db: AsyncSession, current_user: User, opportunity_id: uuid.UUID
) -> JSONResponse | None:
    """404 for an unknown opportunity or one this viewer may not see; the same answer for
    both, so a reviewer cannot probe which ids exist."""
    if await db.get(LandscapeOpportunity, opportunity_id) is None:
        return _error(404, "not_found")
    if not await landscape_view.can_view(db, current_user, opportunity_id):
        return _error(404, "not_found")
    return None


async def _storage_error(
    db: AsyncSession, exc: SQLAlchemyError, route: str, opportunity_id: uuid.UUID,
    user_id: uuid.UUID,
) -> JSONResponse:
    try:
        await db.rollback()
    except SQLAlchemyError:
        pass
    # The class and the ids only: str(exc) of a DBAPIError includes its parameters.
    logger.error(
        "Landscape chat %s: storage error %s (opportunity %s, user %s)",
        route, type(exc).__name__, opportunity_id, user_id,
    )
    return _error(500, "storage_error")


@router.get("/{opportunity_id}")
async def landscape_chat_history(
    opportunity_id: str,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _USER,
) -> JSONResponse:
    """The signed-in user's conversation about this opportunity, in their current tier."""
    user_id = current_user.id
    refused = _refused(request, current_user)
    if refused is not None:
        return refused
    parsed_id = _parse_opportunity_id(opportunity_id)
    if parsed_id is None:
        return _error(404, "not_found")
    try:
        unavailable = await _unavailable(db, current_user, parsed_id)
        if unavailable is not None:
            return unavailable
        # The drawer's poll asks with ?poll=1 and skips the every-user sweep.
        poll = request.query_params.get("poll") == "1"
        payload = await chat.list_history(
            db, landscape_opportunity_id=parsed_id, user=current_user, sweep=not poll
        )
        if payload is None:  # no current assessment in the active epoch
            return _error(404, "not_found")
        await db.commit()  # the stale sweep's writes
    except SQLAlchemyError as exc:
        return await _storage_error(db, exc, "history", parsed_id, user_id)
    return JSONResponse(payload, headers=_NO_STORE)


@router.post("/{opportunity_id}/messages")
async def landscape_chat_ask(
    opportunity_id: str,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _USER,
):
    """Ask one question; the answer streams back as Server-Sent Events."""
    user_id = current_user.id
    refused = _refused(request, current_user)
    if refused is not None:
        return refused
    parsed_id = _parse_opportunity_id(opportunity_id)
    if parsed_id is None:
        return _error(404, "not_found")
    try:
        unavailable = await _unavailable(db, current_user, parsed_id)
        if unavailable is not None:
            return unavailable
        if not _is_json(request):
            return _error(415, "unsupported_media_type")
        # An unrecorded edit of chat.md: refuse rather than answer from it (spec §8.1).
        if not prompt_set.chat_prompt_matches():
            return _error(503, "prompt_unrecorded")
        body = await _body(request)
        prepared = await chat.prepare_turn(
            db,
            landscape_opportunity_id=parsed_id,
            user=current_user,
            question_raw=body.get("question"),
            origin_raw=body.get("origin"),
        )
    except chat.ChatError as err:
        return _error(err.status, err.code, **err.extra)
    except SQLAlchemyError as exc:
        return await _storage_error(db, exc, "ask", parsed_id, user_id)
    # prepare_turn committed: the request's session holds no connection from here on.
    queue = chat.start_turn(prepared)
    return StreamingResponse(
        chat.sse_stream(queue), media_type="text/event-stream", headers=_STREAM_HEADERS
    )


@router.post("/{opportunity_id}/clear")
async def landscape_chat_clear(
    opportunity_id: str,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _USER,
) -> JSONResponse:
    """Delete the signed-in user's turns on this opportunity, every tier. The body is ignored
    beyond its content type."""
    user_id = current_user.id
    refused = _refused(request, current_user)
    if refused is not None:
        return refused
    parsed_id = _parse_opportunity_id(opportunity_id)
    if parsed_id is None:
        return _error(404, "not_found")
    try:
        unavailable = await _unavailable(db, current_user, parsed_id)
        if unavailable is not None:
            return unavailable
        if not _is_json(request):
            return _error(415, "unsupported_media_type")
        deleted = await chat.clear_history(
            db, landscape_opportunity_id=parsed_id, user_id=user_id
        )
    except chat.ChatError as err:
        return _error(err.status, err.code, **err.extra)
    except SQLAlchemyError as exc:
        return await _storage_error(db, exc, "clear", parsed_id, user_id)
    return JSONResponse({"deleted": deleted}, headers=_NO_STORE)


@router.post("/{opportunity_id}/opened")
async def landscape_chat_opened(
    opportunity_id: str,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _USER,
) -> JSONResponse:
    """Count one opening of the drawer (content-free; see ``chat.record_open``). Refused
    exactly as the other routes are, so an impersonated session is never counted."""
    user_id = current_user.id
    refused = _refused(request, current_user)
    if refused is not None:
        return refused
    parsed_id = _parse_opportunity_id(opportunity_id)
    if parsed_id is None:
        return _error(404, "not_found")
    try:
        unavailable = await _unavailable(db, current_user, parsed_id)
        if unavailable is not None:
            return unavailable
        if not _is_json(request):
            return _error(415, "unsupported_media_type")
        body = await _body(request)
        await chat.record_open(
            db, landscape_opportunity_id=parsed_id, user=current_user, via_raw=body.get("via")
        )
    except SQLAlchemyError as exc:
        return await _storage_error(db, exc, "opened", parsed_id, user_id)
    return JSONResponse({"ok": True}, headers=_NO_STORE)
