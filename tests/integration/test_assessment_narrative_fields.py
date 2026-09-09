"""The three reviewer-facing narrative fields on an assessment (migration 0043).

Task 2 covers the columns themselves; Task 8 extends this file with the engine
write path that fills them from the sidecar.
"""

import pytest
from sqlalchemy import select

from src.models import OpportunityAssessment, SimulationRun

pytestmark = pytest.mark.integration


async def _seed_run(db):
    run = SimulationRun()
    db.add(run)
    await db.flush()
    return run


async def test_narrative_fields_round_trip(db_session):
    run = await _seed_run(db_session)
    row = OpportunityAssessment(
        simulation_run_id=run.id,
        agent_id="blackbird",
        channel_name="general",
        company_or_project="Short label",
        headline="A blood test that says who responds to immunotherapy in liver cancer.",
        key_points=["The classifier does not exist yet", "Circadian confound unmeasured"],
        elevator_pitch="Hopkins has 39-plex cytokine data on 124 patients.",
    )
    db_session.add(row)
    await db_session.flush()
    db_session.expunge(row)

    stored = (
        await db_session.execute(
            select(OpportunityAssessment).where(OpportunityAssessment.id == row.id)
        )
    ).scalar_one()
    assert stored.headline.startswith("A blood test")
    assert stored.key_points == [
        "The classifier does not exist yet",
        "Circadian confound unmeasured",
    ]
    assert stored.elevator_pitch.startswith("Hopkins has")


async def test_narrative_fields_default_to_sql_null_not_json_null(db_session):
    """`key_points` is JSONB, and `none_as_null=True` is what keeps Python None
    a real SQL NULL. Without it, `WHERE key_points IS NULL` misses the row —
    the exact defect 0031 and 0036 each had to repair once (A14)."""
    run = await _seed_run(db_session)
    row = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        key_points=None,
    )
    db_session.add(row)
    await db_session.flush()

    found = (
        await db_session.execute(
            select(OpportunityAssessment.id).where(
                OpportunityAssessment.id == row.id,
                OpportunityAssessment.key_points.is_(None),
            )
        )
    ).scalar_one_or_none()
    assert found == row.id
