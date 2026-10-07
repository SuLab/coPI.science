"""One generate_profile enqueue path, and a dead job offers Try Again."""
import pytest
from sqlalchemy import func, select

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_REVIEWER,
    Job,
    User,
)
from src.services.profile_jobs import enqueue_profile_job_if_absent
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _jobs(db_session, user_id, status=None):
    stmt = select(func.count()).select_from(Job).where(
        Job.user_id == user_id, Job.type == "generate_profile",
    )
    if status:
        stmt = stmt.where(Job.status == status)
    return await db_session.scalar(stmt)


async def test_the_helper_returns_the_live_job_and_refuses_only_the_right_accounts(db_session):
    pi = await factories.make_user(db_session, access_status="pending")
    first = await enqueue_profile_job_if_absent(db_session, pi)
    assert first is not None and first.payload == {"user_id": str(pi.id), "orcid": pi.orcid}
    assert await enqueue_profile_job_if_absent(db_session, pi) is first
    first.status = "processing"
    await db_session.flush()
    assert await enqueue_profile_job_if_absent(db_session, pi) is first
    first.status = "dead"
    await db_session.flush()
    assert (await enqueue_profile_job_if_absent(db_session, pi)) is not first

    for kw in ({"user_role": USER_ROLE_MANAGER}, {"user_role": USER_ROLE_REVIEWER},
               {"access_status": "denied"}):
        other = await factories.make_user(db_session, **kw)
        assert await enqueue_profile_job_if_absent(db_session, other) is None
        assert await _jobs(db_session, other.id) == 0


async def test_a_double_profile_refresh_leaves_one_pending_job(client, db_session):
    pi = await factories.make_user(db_session)
    for _ in range(2):
        resp = await client.post("/profile/refresh", headers=auth_headers(pi.id))
        assert resp.status_code == 302
    assert await _jobs(db_session, pi.id, "pending") == 1


async def test_manager_add_pi_then_approval_leaves_exactly_one_pending_job(
    client, db_session, monkeypatch,
):
    async def orcid_profile(orcid_id):
        return {"name": "Added Pi", "orcid": orcid_id, "employments": []}

    monkeypatch.setattr("src.services.pi_onboarding.fetch_orcid_profile", orcid_profile)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    resp = await client.post(
        "/workspace/pis", data={"orcid": "0000-0002-7777-0001"}, headers=auth_headers(manager.id),
    )
    assert resp.status_code == 302
    added = (await db_session.execute(
        select(User).where(User.orcid == "0000-0002-7777-0001")
    )).scalar_one()
    assert await _jobs(db_session, added.id, "pending") == 1
    resp = await client.post(
        f"/admin/access-requests/{added.id}/approve", headers=auth_headers(admin.id),
    )
    assert resp.status_code == 302
    assert await _jobs(db_session, added.id, "pending") == 1, "the approval reused the job"


async def test_the_onboarding_page_offers_try_again_for_a_dead_job(client, db_session):
    pi = await factories.make_user(db_session, onboarding_complete=False)
    db_session.add(Job(
        type="generate_profile", user_id=pi.id, status="dead", attempts=3,
        payload={"user_id": str(pi.id)},
    ))
    await db_session.flush()
    resp = await client.get("/onboarding", headers=auth_headers(pi.id))
    assert resp.status_code == 200
    assert "Try Again" in resp.text and 'action="/onboarding/retry"' in resp.text


async def test_the_onboarding_page_names_a_retry_in_progress(client, db_session):
    pi = await factories.make_user(db_session, onboarding_complete=False)
    db_session.add(Job(
        type="generate_profile", user_id=pi.id, status="pending", attempts=1,
        payload={"user_id": str(pi.id)},
    ))
    await db_session.flush()
    resp = await client.get("/onboarding", headers=auth_headers(pi.id))
    assert "retrying after a temporary error (attempt 2 of 3)" in resp.text
