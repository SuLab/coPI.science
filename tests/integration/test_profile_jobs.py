"""One generate_profile enqueue path, and a dead job offers Try Again."""
import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_REVIEWER,
    Job,
    User,
)
from src.models.job import INTERACTIVE_PRIORITY
from src.services.profile_jobs import enqueue_profile_job_if_absent, enqueue_profile_regeneration
from tests import factories
from tests.flash_support import session_cookie_header
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
        "/workspace/pis", data={"orcid": "0000-0002-7777-0009"}, headers=auth_headers(manager.id),
    )
    assert resp.status_code == 302
    added = (await db_session.execute(
        select(User).where(User.orcid == "0000-0002-7777-0009")
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


async def _past_jobs(db, user, status, *hours_ago):
    now = datetime.now(UTC)
    for hours in hours_ago:
        enqueued = now - timedelta(hours=hours)
        db.add(Job(type="generate_profile", user_id=user.id, status=status, payload={},
                   enqueued_at=enqueued,
                   completed_at=enqueued + timedelta(minutes=5) if status == "completed" else None))
    await db.flush()


async def test_fifth_job_in_window_is_the_last(db_session):
    pi = await factories.make_user(db_session)
    await _past_jobs(db_session, pi, "completed", 1, 2, 3, 4)
    first = await enqueue_profile_regeneration(db_session, pi.id, actor_id=pi.id)
    assert first.status == "queued" and first.recent_jobs == 4
    assert await _jobs(db_session, pi.id) == 5
    first.job.status = "completed"
    await db_session.flush()
    again = await enqueue_profile_regeneration(db_session, pi.id, actor_id=pi.id)
    assert again.status == "limited" and again.job is None
    assert await _jobs(db_session, pi.id) == 5


async def test_job_older_than_24h_does_not_count(db_session):
    pi = await factories.make_user(db_session)
    await _past_jobs(db_session, pi, "completed", 25, 1, 2, 3, 4)
    assert (await enqueue_profile_regeneration(db_session, pi.id, actor_id=None)).status == "queued"


async def test_in_flight_is_not_refused_and_lifts_priority(db_session):
    pi = await factories.make_user(db_session)
    await _past_jobs(db_session, pi, "completed", 1, 2, 3, 4, 5)
    pending = Job(type="generate_profile", user_id=pi.id, status="pending", payload={},
                  priority=-10)
    db_session.add(pending)
    await db_session.flush()
    out = await enqueue_profile_regeneration(db_session, pi.id, actor_id=None)
    assert out.status == "in_flight"
    assert await _jobs(db_session, pi.id) == 6
    await db_session.refresh(pending)
    assert pending.priority == INTERACTIVE_PRIORITY


async def test_manager_or_denied_is_not_eligible(db_session):
    for kw in ({"user_role": USER_ROLE_MANAGER}, {"access_status": "denied"}):
        user = await factories.make_user(db_session, **kw)
        out = await enqueue_profile_regeneration(db_session, user.id, actor_id=None)
        assert out.status == "not_eligible" and out.job is None
        assert await _jobs(db_session, user.id) == 0


async def test_concurrent_calls_insert_one_job(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        user = User(name="Cap Race", access_status="allowed",
                    orcid=f"0000-0006-{uuid.uuid4().int % 10000:04d}-0007")
        s.add(user)
        await s.flush()
        now = datetime.now(UTC)
        s.add_all([Job(type="generate_profile", user_id=user.id, status="completed", payload={},
                       enqueued_at=now - timedelta(hours=h)) for h in (1, 2, 3, 4)])
        await s.commit()
    try:
        async def one():
            async with factory() as s:
                out = await enqueue_profile_regeneration(s, user.id, actor_id=None)
                await s.commit()
                return out.status

        assert sorted(await asyncio.gather(one(), one())) == ["in_flight", "queued"]
        async with factory() as s:
            assert await s.scalar(
                select(func.count()).select_from(Job).where(Job.user_id == user.id)) == 5
    finally:
        async with factory() as s:
            await s.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
            await s.commit()


async def test_operator_enqueue_is_not_capped(db_session):
    pi = await factories.make_user(db_session)
    await _past_jobs(db_session, pi, "completed", 1, 2, 3, 4, 5)
    assert await enqueue_profile_job_if_absent(db_session, pi) is not None
    assert await _jobs(db_session, pi.id) == 6


async def test_profile_refresh_limited_flashes_and_enqueues_nothing(client, db_session):
    pi = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=pi)
    await _past_jobs(db_session, pi, "completed", 1, 2, 3, 4, 5)
    resp = await client.post("/profile/refresh", headers=auth_headers(pi.id))
    assert resp.status_code == 302 and resp.headers["location"] == "/profile"
    page = await client.get("/profile", headers=session_cookie_header(resp))
    assert "Too many profile refreshes today" in page.text
    assert await _jobs(db_session, pi.id) == 5


async def test_profile_refresh_under_cap_queues(client, db_session):
    pi = await factories.make_user(db_session)
    resp = await client.post("/profile/refresh", headers=auth_headers(pi.id))
    assert resp.status_code == 302 and resp.headers["location"] == "/profile?refreshing=1"
    [job] = (await db_session.execute(select(Job).where(
        Job.user_id == pi.id, Job.type == "generate_profile"))).scalars().all()
    assert job.status == "pending" and job.priority == INTERACTIVE_PRIORITY
