"""The claim protocol of a hub decline's #assessments-summary headline."""
from datetime import timedelta

import pytest
from sqlalchemy import func, update

from src.models import HubDecline
from src.services import decline_claims
from tests import factories

pytestmark = pytest.mark.integration


async def _decline(db, run, thread="1.1"):
    row = HubDecline(simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="hu",
                     channel_name="general", thread_id=thread, slack_ts="2.2",
                     reason_text="⏸️ Declining.")
    db.add(row)
    await db.flush()
    return row


async def test_claim_post_and_release(db_session):
    run = await factories.make_simulation_run(db_session)
    row = await _decline(db_session, run)
    assert [r.id for r in await decline_claims.owed(db_session, run.id)] == [row.id]
    assert await decline_claims.claim(db_session, row.id) is True
    assert await decline_claims.claim(db_session, row.id) is False  # already claimed
    assert await decline_claims.owed(db_session, run.id) == []
    await decline_claims.release_claim(db_session, row.id)
    assert await decline_claims.claim(db_session, row.id) is True
    await decline_claims.mark_posted(db_session, row.id)
    await decline_claims.release_claim(db_session, row.id)  # a posted row keeps its claim
    await db_session.refresh(row)
    assert row.summary_posted_at is not None and row.summary_claimed_at is not None
    assert await decline_claims.claim(db_session, row.id) is False


async def test_an_old_claim_with_no_post_is_in_doubt(db_session):
    run = await factories.make_simulation_run(db_session)
    row = await _decline(db_session, run)
    assert await decline_claims.claim(db_session, row.id)
    assert await decline_claims.list_in_doubt(db_session, run.id) == []
    await db_session.execute(update(HubDecline).where(HubDecline.id == row.id).values(
        summary_claimed_at=func.now() - timedelta(hours=1)))
    assert [r.id for r in await decline_claims.list_in_doubt(db_session, run.id)] == [row.id]
