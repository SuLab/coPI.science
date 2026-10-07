"""Guarded legacy GET destinations. Writes are registered against shared callables."""

import uuid

from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from src.web.urls import legacy_query, page_url


async def require_resource(db, model, resource_id: uuid.UUID, message: str):
    if await db.scalar(select(model.id).where(model.id == resource_id)) is None:
        raise HTTPException(status_code=404, detail=message)


def redirect(request, user, feature, route_name, *, manager=False, fragment="", **params):
    return RedirectResponse(
        page_url(
            request,
            route_name,
            query=legacy_query(request, feature, staff=user.is_staff, manager=manager),
            fragment=fragment,
            **params,
        ),
        status_code=302,
    )
