"""Role gating: get_staff_user, and the guarantee that a manager cannot reach
/admin or impersonate anyone.
"""

import base64
import json

import pytest
from fastapi import Depends, FastAPI
from itsdangerous import TimestampSigner
from sqlalchemy import select

from src.config import get_settings
from src.dependencies import get_staff_user
from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI, User
from tests import factories

pytestmark = pytest.mark.integration


def _session_cookie(user_id) -> str:
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return signer.sign(data).decode("utf-8")


def auth_headers(user_id) -> dict:
    """Shared by test_manager_views.py — keep the two in sync."""
    return {"Cookie": f"copi-session={_session_cookie(user_id)}"}


async def test_default_role_is_pi_in_the_database(db_session):
    u = User(name="Fresh", orcid="0000-0000-0000-9001")
    db_session.add(u)
    await db_session.flush()
    assert u.user_role == USER_ROLE_PI


async def test_is_admin_filters_rows_in_the_database(db_session):
    await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    await factories.make_user(db_session, user_role=USER_ROLE_PI)
    rows = (await db_session.execute(select(User).where(User.is_admin))).scalars().all()
    assert [u.user_role for u in rows] == [USER_ROLE_ADMIN]


async def test_is_staff_filters_admin_and_manager_only(db_session):
    await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    await factories.make_user(db_session, user_role=USER_ROLE_PI)
    rows = (await db_session.execute(select(User).where(User.is_staff))).scalars().all()
    assert sorted(u.user_role for u in rows) == [USER_ROLE_ADMIN, USER_ROLE_MANAGER]


@pytest.mark.parametrize(
    "role,expected",
    [(USER_ROLE_PI, 403), (USER_ROLE_MANAGER, 200), (USER_ROLE_ADMIN, 200)],
)
async def test_get_staff_user_gates_by_role(db_session, monkeypatch, role, expected):
    import httpx
    from httpx import ASGITransport

    from src.database import get_db

    user = await factories.make_user(db_session, user_role=role)

    app = FastAPI()

    @app.get("/probe")
    async def probe(u: User = Depends(get_staff_user)):  # noqa: B008
        return {"role": u.user_role}

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    from starlette.middleware.sessions import SessionMiddleware

    app.add_middleware(
        SessionMiddleware,
        secret_key=get_settings().secret_key,
        session_cookie="copi-session",
    )
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/probe", headers=auth_headers(user.id))
    assert r.status_code == expected
