"""Job progress entries appended AFTER a flush must still reach the database.

``Job.payload`` is a plain JSON column with no mutation tracking, and the old
pipeline closure reassigned the payload only on its FIRST call — every later
in-place append after a ``db.flush()`` (the first sits at the Publication
persistence step) never marked the attribute dirty and was silently dropped at
commit. Flags that only make sense late in the run (``tenure_unknown``,
coverage notes) were exactly the ones lost.
"""

import pytest
from sqlalchemy import select

from src.models import Job
from src.services.profile_pipeline import append_job_progress
from tests import factories

pytestmark = pytest.mark.integration


async def test_progress_appended_after_a_flush_survives_commit(db_session):
    user = await factories.make_user(db_session, orcid="0000-0009-1111-2222")
    job = Job(
        type="generate_profile",
        user_id=user.id,
        payload={"user_id": str(user.id)},
    )
    db_session.add(job)
    await db_session.flush()

    job_id = job.id

    append_job_progress(job, "step1", "first")
    await db_session.flush()
    append_job_progress(job, "step2", "second")
    await db_session.commit()
    db_session.expire_all()

    row = (
        await db_session.execute(select(Job).where(Job.id == job_id))
    ).scalar_one()
    steps = [p["step"] for p in row.payload.get("progress", [])]
    assert steps == ["step1", "step2"], (
        "an append after a flush must be written at commit, not lost to "
        "JSON mutation-tracking"
    )
