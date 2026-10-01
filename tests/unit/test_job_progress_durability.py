"""job_progress.record appends in its own short transaction; entries survive a
later rollback of the caller's transaction (they no longer ride on it)."""
import pytest

from src.models import Job
from src.services import job_progress
from tests import factories

pytestmark = pytest.mark.integration


@pytest.mark.usefixtures("progress_on_test_connection")
async def test_entries_append_in_order(db_session):
    user = await factories.make_user(db_session, orcid="0000-0009-1111-2222")
    job = Job(type="generate_profile", user_id=user.id, payload={"user_id": str(user.id)})
    db_session.add(job)
    await db_session.flush()
    await job_progress.record(job.id, "step1", "first")
    await job_progress.record(job.id, "step2", "second")
    await db_session.refresh(job)
    assert job.payload["progress"] == [
        {"step": "step1", "detail": "first"},
        {"step": "step2", "detail": "second"},
    ]
    assert job.payload["user_id"] == str(user.id)


async def test_record_without_a_job_is_a_no_op():
    await job_progress.record(None, "x", "y")
