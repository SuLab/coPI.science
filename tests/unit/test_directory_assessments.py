"""An incomplete-panel verdict must be visibly distinct from a vetted one.

Storing it (Task 3) is only safe if the page says so — otherwise Task 3 turns
a loud refusal into a silent, ordinary-looking row.
"""

import pytest

from src.models.opportunity import OpportunityAssessment
from src.services.directory import list_assessments
from tests import factories


@pytest.mark.asyncio
async def test_list_assessments_counts_incomplete_panels(db_session):
    run = await factories.make_simulation_run(db_session)
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird",
            subject_agent_id="gordy", channel_name="general",
            recommendation="conditional",
            panel_incomplete=True, missing_domains=["chemistry"],
        )
    )
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird",
            subject_agent_id="wu", channel_name="general",
            recommendation="pass", panel_incomplete=False,
        )
    )
    await db_session.commit()

    view = await list_assessments(db_session, str(run.id))
    assert view["incomplete_panel_count"] == 1


@pytest.mark.asyncio
async def test_list_assessments_incomplete_panel_count_is_scoped_to_the_selected_run(db_session):
    """The single-run test above cannot see a dropped run-scope guard: with
    only one ``SimulationRun`` in the database, a correctly-scoped count and a
    completely unscoped count both return 1. This seeds a SECOND run with its
    own ``panel_incomplete=True`` row and asserts the first run's count does
    not pick it up — the exact failure mode of one run's triage page
    reporting another run's incomplete panels.
    """
    run = await factories.make_simulation_run(db_session)
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird",
            subject_agent_id="gordy", channel_name="general",
            recommendation="conditional",
            panel_incomplete=True, missing_domains=["chemistry"],
        )
    )

    other_run = await factories.make_simulation_run(db_session)
    assert other_run.id != run.id
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=other_run.id, agent_id="blackbird",
            subject_agent_id="wu", channel_name="general",
            recommendation="conditional",
            panel_incomplete=True, missing_domains=["biology"],
        )
    )
    await db_session.commit()

    view = await list_assessments(db_session, str(run.id))
    assert view["incomplete_panel_count"] == 1

    other_view = await list_assessments(db_session, str(other_run.id))
    assert other_view["incomplete_panel_count"] == 1

    all_view = await list_assessments(db_session, "all")
    assert all_view["incomplete_panel_count"] == 2
