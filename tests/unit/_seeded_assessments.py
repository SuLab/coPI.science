"""Shared fixture for the Task 32/34 differential tests: a run with a few
assessments (stamped and unstamped, with and without scores, gating and red flags)."""
import pytest_asyncio

from src.models import OpportunityAssessment
from tests import factories


@pytest_asyncio.fixture
async def seeded_assessments(db_session):
    """Returns the ids of the seeded assessments."""
    run = await factories.make_simulation_run(db_session)
    rows = [
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
            channel_name="general", company_or_project="Scored",
            scores={"team_executability": 4, "novelty": 3.5, "legacy_dim": 2},
            dimension_rationales={"team_executability": "strong team"},
            gating={"ip_clear": "met", "x": "not_met", "y": "unconfirmed"},
            red_flags=["A long flag. With a second sentence."],
        ),
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="lab1",
            channel_name="general", company_or_project="No scores", scores=None,
        ),
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird", subject_agent_id=None,
            channel_name="general", company_or_project="Empty", scores={}, gating={}, red_flags=[],
        ),
    ]
    for row in rows:
        db_session.add(row)
    await db_session.flush()
    return [row.id for row in rows]
