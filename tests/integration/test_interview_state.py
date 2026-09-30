"""Spec P0-04: an interview has ENDED exactly when a ThreadDecision exists for
(run, thread). Evictions write no decision, so an evicted thread counts as open."""
import pytest

from src.services.interview_state import ended_thread_ids
from tests import factories

pytestmark = pytest.mark.integration


async def test_a_thread_decision_is_the_only_evidence_of_an_ended_interview(db_session):
    run = await factories.make_simulation_run(db_session)
    other_run = await factories.make_simulation_run(db_session)
    await factories.make_thread_decision(db_session, run=run, thread_id="t-ended")
    await factories.make_thread_decision(db_session, run=run, thread_id="t-ended")  # repeated rows
    await factories.make_thread_decision(db_session, run=other_run, thread_id="t-open")

    assert await ended_thread_ids(db_session, run.id) == {"t-ended"}
    assert await ended_thread_ids(db_session, run.id, ["t-ended", "t-open"]) == {"t-ended"}
    assert await ended_thread_ids(db_session, run.id, []) == set()
