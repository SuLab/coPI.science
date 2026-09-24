"""The assessment chat's three routes (docs/specs/2026-09-24-assessment-chat-design.md §6).

Mounted at /assessment-chat for admin, manager and reviewer alike. Every route, in
order: 403 while impersonating (a history is private, and asking spends money — the
reviews router's `generate_prompt_suggestions` precedent), 403 for a signed-in user
who is neither staff nor reviewer, 503 when the chat is switched off, 404 for an
unknown or malformed assessment id, and for both POSTs 415 unless the body is JSON:
FastAPI parses a body with no content type as JSON, and a sibling tenant's `no-cors`
fetch sends none, so this closes that path in addition to OriginGuardMiddleware. No
conversation or turn id is ever taken from the client — every read and write is keyed
on (assessment, signed-in user, tier). Every response carries `Cache-Control:
no-store`, and every handler catches SQLAlchemyError at its boundary so a failed write
never reaches Starlette's error log with its bound parameters (which include the
question).

Routes depend on `get_current_user`, not `get_review_user` (SW-2): `get_current_user`
returns the IMPERSONATED user, so a router- or route-level `get_review_user` would
403 an impersonating admin with "Review access required" before `_refused` ever gets
a chance to answer `{"error": "impersonating"}` — the drawer then shows a generic
failure instead of the impersonation message it knows how to render. `_refused` checks
impersonation first and only then applies `get_review_user`'s own predicate by hand.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.dependencies import get_current_user
from src.models import OpportunityAssessment, User
from src.services import assessment_chat as chat

logger = logging.getLogger(__name__)
router = APIRouter()

_DB = Depends(get_db)
_USER = Depends(get_current_user)

_NO_STORE = {"Cache-Control": "no-store"}
#: `no-transform` and `X-Accel-Buffering: no` are for org1's nginx, which buffers a
#: proxied response unless the response says not to (F2).
_STREAM_HEADERS = {"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"}


def _error(status: int, code: str, **extra: object) -> JSONResponse:
    return JSONResponse({"error": code, **extra}, status_code=status, headers=_NO_STORE)


def _refused(request: Request, current_user: User) -> JSONResponse | None:
    # The attribute exists only on an impersonated user object (src/dependencies.py).
    if getattr(current_user, "_is_impersonated", False):
        return _error(403, "impersonating")
    # Same predicate as get_review_user (src/dependencies.py) — kept in sync by hand,
    # since we can't depend on get_review_user itself without losing the ordering
    # above.
    if not (current_user.is_staff or current_user.is_reviewer):
        return _error(403, "forbidden")
    if not getattr(request.app.state, "assessment_chat_enabled", False):
        return _error(503, "disabled")
    return None


def _parse_assessment_id(assessment_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(assessment_id)
    except ValueError:
        return None


def _is_json(request: Request) -> bool:
    content_type = request.headers.get("content-type", "")
    return content_type.split(";", 1)[0].strip().lower() == "application/json"


async def _exists(db: AsyncSession, assessment_id: uuid.UUID) -> bool:
    found = await db.scalar(
        select(OpportunityAssessment.id).where(OpportunityAssessment.id == assessment_id)
    )
    return found is not None


async def _question(request: Request) -> object:
    try:
        body = await request.json()
    except ValueError:  # malformed JSON, or bytes that are not UTF-8
        return None
    return body.get("question") if isinstance(body, dict) else None


async def _storage_error(
    db: AsyncSession, exc: SQLAlchemyError, route: str, assessment_id: uuid.UUID, user_id: uuid.UUID
) -> JSONResponse:
    try:
        await db.rollback()
    except SQLAlchemyError:
        pass
    # The class and the ids only: str(exc) of a DBAPIError includes its parameters.
    logger.error(
        "Assessment chat %s: storage error %s (assessment %s, user %s)",
        route, type(exc).__name__, assessment_id, user_id,
    )
    return _error(500, "storage_error")


@router.get("/{assessment_id}")
async def assessment_chat_history(
    assessment_id: str,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _USER,
) -> JSONResponse:
    """The signed-in user's conversation about this assessment, in their current tier."""
    user_id = current_user.id
    refused = _refused(request, current_user)
    if refused is not None:
        return refused
    parsed_id = _parse_assessment_id(assessment_id)
    if parsed_id is None:
        return _error(404, "not_found")
    try:
        payload = await chat.list_history(db, assessment_id=parsed_id, user=current_user)
        if payload is None:
            return _error(404, "not_found")
        await db.commit()  # the stale sweep's writes
    except SQLAlchemyError as exc:
        return await _storage_error(db, exc, "history", parsed_id, user_id)
    return JSONResponse(payload, headers=_NO_STORE)


@router.post("/{assessment_id}/messages")
async def assessment_chat_ask(
    assessment_id: str,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _USER,
):
    """Ask one question; the answer streams back as Server-Sent Events (§6.4)."""
    user_id = current_user.id
    refused = _refused(request, current_user)
    if refused is not None:
        return refused
    parsed_id = _parse_assessment_id(assessment_id)
    if parsed_id is None:
        return _error(404, "not_found")
    try:
        if not await _exists(db, parsed_id):
            return _error(404, "not_found")
        if not _is_json(request):
            return _error(415, "unsupported_media_type")
        prepared = await chat.prepare_turn(
            db, assessment_id=parsed_id, user=current_user, question_raw=await _question(request)
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


@router.post("/{assessment_id}/clear")
async def assessment_chat_clear(
    assessment_id: str,
    request: Request,
    db: AsyncSession = _DB,
    current_user: User = _USER,
) -> JSONResponse:
    """Delete the signed-in user's turns on this assessment, every tier (D16). The body
    is ignored beyond its content type."""
    user_id = current_user.id
    refused = _refused(request, current_user)
    if refused is not None:
        return refused
    parsed_id = _parse_assessment_id(assessment_id)
    if parsed_id is None:
        return _error(404, "not_found")
    try:
        if not await _exists(db, parsed_id):
            return _error(404, "not_found")
        if not _is_json(request):
            return _error(415, "unsupported_media_type")
        deleted = await chat.clear_history(db, assessment_id=parsed_id, user_id=user_id)
    except chat.ChatError as err:
        return _error(err.status, err.code, **err.extra)
    except SQLAlchemyError as exc:
        return await _storage_error(db, exc, "clear", parsed_id, user_id)
    return JSONResponse({"deleted": deleted}, headers=_NO_STORE)
