from datetime import UTC, datetime, timedelta

import pytest

from src.models import Job
from src.models.job import BULK_PRIORITY, INTERACTIVE_PRIORITY
from src.services.profile_jobs import enqueue_profile_job_if_absent
from src.worker.main import claim_job
from tests import factories

pytestmark = pytest.mark.asyncio


async def test_claim_order_is_priority_then_age(db_session):
    u1, u2, u3 = [await factories.make_user(db_session) for _ in range(3)]
    t0 = datetime.now(UTC) - timedelta(hours=1)
    db_session.add_all([
        Job(type="enrich_grants", user_id=u1.id, payload={}, priority=BULK_PRIORITY, enqueued_at=t0),
        Job(type="enrich_grants", user_id=u2.id, payload={}, priority=None, enqueued_at=t0 + timedelta(minutes=1)),
        Job(type="generate_profile", user_id=u3.id, payload={}, priority=INTERACTIVE_PRIORITY,
            enqueued_at=t0 + timedelta(minutes=2)),
    ])
    await db_session.flush()
    first = await claim_job(db_session)
    second = await claim_job(db_session)
    third = await claim_job(db_session)
    assert [first.user_id, second.user_id, third.user_id] == [u3.id, u2.id, u1.id]


async def test_helper_writes_the_priority(db_session):
    user = await factories.make_user(db_session)
    job = await enqueue_profile_job_if_absent(db_session, user, priority=INTERACTIVE_PRIORITY)
    assert job.priority == INTERACTIVE_PRIORITY


async def test_helper_default_is_null(db_session):
    user = await factories.make_user(db_session)
    job = await enqueue_profile_job_if_absent(db_session, user)
    assert job.priority is None


def test_constants():
    assert (INTERACTIVE_PRIORITY, BULK_PRIORITY) == (10, -10)


async def test_an_interactive_enqueue_raises_a_pending_bulk_rows_priority(db_session):
    """Bulk queued first, then the person arrives: their wait is interactive."""
    user = await factories.make_user(db_session)
    first = await enqueue_profile_job_if_absent(db_session, user, priority=BULK_PRIORITY)
    again = await enqueue_profile_job_if_absent(db_session, user, priority=INTERACTIVE_PRIORITY)
    await db_session.refresh(first)
    assert again.id == first.id and first.priority == INTERACTIVE_PRIORITY
    # A lower priority never lowers it.
    await enqueue_profile_job_if_absent(db_session, user, priority=BULK_PRIORITY)
    await db_session.refresh(first)
    assert first.priority == INTERACTIVE_PRIORITY


async def test_an_interactive_enqueue_lifts_an_openalex_gate_deferral(db_session):
    """The OpenAlex gate binds BULK jobs only: once the person asks, a gate-deferred row runs
    now; any other deferral keeps its time (semantic review 2026-10-06)."""
    later = datetime.now(UTC) + timedelta(hours=10)
    gated, other = await factories.make_user(db_session), await factories.make_user(db_session)
    rows = {}
    for user, reason in ((gated, "OpenAlex free budget: 90 of 1000 credits left, 100 kept in reserve"),
                         (other, "company discovery: the COI budget is spent")):
        job = await enqueue_profile_job_if_absent(db_session, user, priority=BULK_PRIORITY)
        job.not_before, job.last_error = later, f"deferred until {later.isoformat()}: {reason}"
        rows[user.id] = job
    await db_session.flush()
    for user in (gated, other):
        await enqueue_profile_job_if_absent(db_session, user, priority=INTERACTIVE_PRIORITY)
        await db_session.refresh(rows[user.id])
    assert rows[gated.id].not_before is None and rows[gated.id].priority == INTERACTIVE_PRIORITY
    assert rows[other.id].not_before == later
