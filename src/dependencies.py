"""FastAPI dependencies for auth and DB access."""

import uuid
import logging
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_db
from src.models import User

logger = logging.getLogger(__name__)


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Auth dependency. Checks session cookie for user_id.
    Handles impersonation via copi-impersonate cookie (admin only).
    """
    user_id_str = request.session.get("user_id")
    if not user_id_str:
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/login"},
        )

    try:
        user_id = uuid.UUID(user_id_str)
    except ValueError:
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/login"},
        )

    result = await db.execute(select(User).where(User.id == user_id))
    session_user = result.scalar_one_or_none()

    if session_user is None:
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/login"},
        )

    # Impersonation: admin can view as another user
    impersonate_id = request.cookies.get("copi-impersonate")
    if impersonate_id and session_user.is_admin:
        try:
            imp_uuid = uuid.UUID(impersonate_id)
            result = await db.execute(select(User).where(User.id == imp_uuid))
            imp_user = result.scalar_one_or_none()
            if imp_user:
                # Tag so templates can show impersonation banner
                imp_user._is_impersonated = True  # type: ignore[attr-defined]
                imp_user._real_admin = session_user  # type: ignore[attr-defined]
                return imp_user
        except (ValueError, Exception) as exc:
            logger.warning("Invalid impersonate cookie: %s", exc)

    return session_user


async def get_admin_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Dependency that requires admin status."""
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user
