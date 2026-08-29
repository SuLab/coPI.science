"""Reviewer-role predicates, login/onboarding routing, and PI-write denial.

A reviewer (Task 1's `USER_ROLE_REVIEWER` / `User.is_reviewer`) can score and
comment on assessments but is neither staff (no manager/admin surfaces) nor a
PI (no lab profile or agent). This file pins:

  - `get_review_user` (new): admin, manager, OR reviewer; still refuses a PI.
  - `get_staff_user` still refuses a reviewer (is_staff deliberately excludes
    it — a reviewer must never reach manager writes, discussions, activity).
  - `get_pi_user` now also refuses a reviewer, alongside the existing manager
    denial (additive denylist — admins keep every PI write).
  - Login/onboarding/profile GETs bounce a reviewer to /manager/assessments
    rather than rendering a PI page it can never complete.

The full follow-the-chain-to-200 assertion on /manager/assessments belongs to
Task 3, once the manager router actually admits reviewers; here we only assert
the redirect itself (302 + Location), per the task-2 brief.
"""

import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import func, select

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    Job,
    ResearcherProfile,
    User,
)
from tests import factories
from tests.integration.test_manager_access import auth_headers
from tests.integration.test_pi_only_writes import (
    _IDS,
    PI_ONLY_WRITES,
    _save_profile_form,
    _snapshot,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "role,expected",
    [
        (USER_ROLE_PI, 403),
        (USER_ROLE_MANAGER, 200),
        (USER_ROLE_ADMIN, 200),
        (USER_ROLE_REVIEWER, 200),
    ],
)
async def test_get_review_user_gates_by_role(db_session, monkeypatch, role, expected):
    """Throwaway-app probe cloned from
    test_manager_access.py::test_get_staff_user_gates_by_role, targeting the
    new get_review_user instead."""
    import httpx
    from httpx import ASGITransport

    from src.config import get_settings
    from src.database import get_db
    from src.dependencies import get_review_user

    user = await factories.make_user(db_session, user_role=role)

    app = FastAPI()

    @app.get("/probe")
    async def probe(u: User = Depends(get_review_user)):  # noqa: B008
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


async def test_get_staff_user_still_refuses_a_reviewer(db_session, monkeypatch):
    """Same probe, but against get_staff_user: a reviewer must stay off the
    manager router entirely, not just off get_pi_user."""
    import httpx
    from httpx import ASGITransport

    from src.config import get_settings
    from src.database import get_db
    from src.dependencies import get_staff_user

    user = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)

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
    assert r.status_code == 403


@pytest.mark.parametrize("path,build", PI_ONLY_WRITES, ids=_IDS)
async def test_a_reviewer_is_refused_every_pi_write(client, db_session, path, build):
    """Clone of test_pi_only_writes.py::test_a_manager_is_refused_every_pi_write
    for a reviewer: all four PI_ONLY_WRITES 403, state unchanged."""
    rev = await factories.make_user(
        db_session, user_role=USER_ROLE_REVIEWER, onboarding_complete=True
    )
    await factories.make_profile(db_session, user=rev, research_summary="untouched")
    await db_session.flush()

    before = await _snapshot(db_session, rev.id)
    r = await client.post(path, data=build(rev), headers=auth_headers(rev.id))
    assert r.status_code == 403, f"{path} served a reviewer"
    assert await _snapshot(db_session, rev.id) == before, f"{path} acted for a reviewer"


async def test_a_reviewer_cannot_save_a_pi_profile(client, db_session):
    """Clone of test_pi_only_writes.py::test_a_manager_cannot_save_a_pi_profile
    for a reviewer: POST /profile/save is the fifth PI write."""
    rev = await factories.make_user(
        db_session, user_role=USER_ROLE_REVIEWER, name="Rhonda Reviewer"
    )
    original_email = rev.email
    await db_session.flush()

    form = {**_save_profile_form(rev), "email": "reviewer-rewrote-this@example.edu"}
    r = await client.post("/profile/save", data=form, headers=auth_headers(rev.id))
    assert r.status_code == 403, "POST /profile/save served a reviewer"

    assert await db_session.scalar(
        select(func.count(ResearcherProfile.id)).where(ResearcherProfile.user_id == rev.id)
    ) == 0, "a reviewer got a ResearcherProfile out of /profile/save"
    assert await db_session.scalar(
        select(User.email).where(User.id == rev.id)
    ) == original_email, "a reviewer rewrote their own email via /profile/save"

    # Control: the same request from a PI must still do all of that.
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Percy Pi Two")
    await db_session.flush()
    pi_form = {**_save_profile_form(pi), "email": "pi-rewrote-this-too@example.edu"}
    r2 = await client.post("/profile/save", data=pi_form, headers=auth_headers(pi.id))
    assert r2.status_code == 302, "POST /profile/save refused a PI"
    assert await db_session.scalar(
        select(func.count(ResearcherProfile.id)).where(ResearcherProfile.user_id == pi.id)
    ) == 1
    assert await db_session.scalar(
        select(User.email).where(User.id == pi.id)
    ) == "pi-rewrote-this-too@example.edu"


async def test_reviewer_visiting_onboarding_is_bounced_and_enqueues_no_job(
    client, db_session
):
    rev = await factories.make_user(
        db_session, user_role=USER_ROLE_REVIEWER, onboarding_complete=False
    )
    r = await client.get(
        "/onboarding", headers=auth_headers(rev.id), follow_redirects=False
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/manager/assessments"

    count = await db_session.scalar(
        select(func.count(Job.id)).where(
            Job.user_id == rev.id, Job.type == "generate_profile"
        )
    )
    assert count == 0


async def test_reviewer_profile_and_edit_pages_redirect(client, db_session):
    """The follow-the-full-chain-to-200 assertion belongs to Task 3, when the
    manager router actually admits reviewers — asserting 200 here cannot pass."""
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)

    r1 = await client.get(
        "/profile", headers=auth_headers(rev.id), follow_redirects=False
    )
    assert r1.status_code == 302
    assert r1.headers["location"] == "/manager/assessments"

    r2 = await client.get(
        "/profile/edit", headers=auth_headers(rev.id), follow_redirects=False
    )
    assert r2.status_code == 302
    assert r2.headers["location"] == "/manager/assessments"
