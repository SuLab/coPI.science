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
    """One gapped verdict, one the floor cleared — only the first is counted.

    The `pass` row now has to STATE its panel columns. It used to leave them at
    their defaults, which after migration 0036 means `panel_owed IS NULL` — "we
    do not know whether any floor ran" — and that is one of the states this
    count exists to surface. A fixture that means "a clean row" has to say so
    since the column exists; the widened count is covered by
    `test_the_run_warning_counts_unverified_and_unrecorded_panels_too` below.
    """
    run = await factories.make_simulation_run(db_session)
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird",
            subject_agent_id="gordy", channel_name="general",
            recommendation="conditional",
            panel_incomplete=True, missing_domains=["chemistry"], panel_owed=True,
        )
    )
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird",
            subject_agent_id="wu", channel_name="general",
            recommendation="pass", panel_incomplete=False, panel_owed=False,
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


@pytest.mark.asyncio
async def test_dimension_stats_expose_the_constant_dimensions(db_session):
    """Four dimensions never exceeded 2 across all 18 production assessments,
    pinning 23 of 100 weight points near minimum. That is invisible on a page
    that only shows totals."""
    run = await factories.make_simulation_run(db_session)
    for score in (1, 2, 2):
        db_session.add(
            OpportunityAssessment(
                simulation_run_id=run.id, agent_id="blackbird",
                subject_agent_id="wu", channel_name="general",
                band="pass",
                scores={"ip_fto": score, "differentiation": 5},
            )
        )
    await db_session.commit()

    view = await list_assessments(db_session, str(run.id))
    stats = {d["dimension"]: d for d in view["dimension_stats"]}

    assert stats["ip_fto"]["max"] == 2
    assert stats["differentiation"]["max"] == 5
    assert stats["ip_fto"]["specialist"] == "legal", (
        "maps_to_dimensions has never had a runtime read; this is it"
    )
    assert view["band_counts"] == [("pass", 3)]


@pytest.mark.asyncio
async def test_the_run_warning_counts_unverified_and_unrecorded_panels_too(db_session):
    """The warning must cover every row whose panel is not VERIFIED, not just
    the ones with a demonstrated gap.

    Three rows are unvetted for three different reasons and exactly one is not:

    * a demonstrated gap (`panel_incomplete=True`),
    * a floor that could not be checked at all (`missing_domains=[]`, the
      ordinary post-restart state — production's normal exit is a SIGKILL),
    * a row that does not record whether a panel was owed (`panel_owed=None`,
      which is EVERY row written before migration 0036 and is deliberately not
      backfilled).

    Counting only the first excluded the other two by construction, which is how
    12 production rows sat behind a banner that said nothing about them and a
    detail page that called them verified. `not_owed` — the floor's own recorded
    exemption — is the one state that is genuinely fine and stays uncounted.
    """
    run = await factories.make_simulation_run(db_session)
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird",
            subject_agent_id="gordy", channel_name="general",
            recommendation="conditional",
            panel_incomplete=True, missing_domains=["chemistry"], panel_owed=True,
        )
    )
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird",
            subject_agent_id="wang", channel_name="general",
            recommendation="conditional",
            panel_incomplete=False, missing_domains=[], panel_owed=True,
        )
    )
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird",
            subject_agent_id="pearce", channel_name="general",
            recommendation="route-to-incubation",
            panel_incomplete=False, missing_domains=None, panel_owed=None,
        )
    )
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird",
            subject_agent_id="wu", channel_name="general",
            recommendation="pass",
            panel_incomplete=False, missing_domains=None, panel_owed=False,
        )
    )
    await db_session.commit()

    view = await list_assessments(db_session, str(run.id))
    assert view["incomplete_panel_count"] == 3


@pytest.mark.asyncio
async def test_a_verified_panel_is_not_counted_as_unvetted(db_session):
    """The other direction, and the reason the count is not simply "every row":
    a row the floor recorded evaluating, with no gap, is vetted."""
    run = await factories.make_simulation_run(db_session)
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird",
            subject_agent_id="gordy", channel_name="general",
            recommendation="conditional",
            panel_incomplete=False, missing_domains=None, panel_owed=True,
        )
    )
    await db_session.commit()

    view = await list_assessments(db_session, str(run.id))
    assert view["incomplete_panel_count"] == 0


@pytest.mark.asyncio
async def test_each_listed_row_carries_its_own_panel_state(db_session):
    """The banner is a number; the table is where a reader decides what to open.
    Every row therefore carries the SAME five-state finding the detail page
    renders, computed by the one definition
    (`src.services.assessment_detail.panel_state`) rather than re-derived in
    Jinja from three columns.
    """
    run = await factories.make_simulation_run(db_session)
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird",
            subject_agent_id="gordy", channel_name="general",
            company_or_project="Gapped Co", recommendation="conditional",
            panel_incomplete=True, missing_domains=["chemistry"], panel_owed=True,
        )
    )
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird",
            subject_agent_id="pearce", channel_name="general",
            company_or_project="Unrecorded Co", recommendation="route-to-incubation",
            panel_incomplete=False, missing_domains=None, panel_owed=None,
        )
    )
    await db_session.commit()

    view = await list_assessments(db_session, str(run.id))
    states = {a.company_or_project: a.panel_state for a in view["assessments"]}
    assert states == {"Gapped Co": "gap", "Unrecorded Co": "unrecorded"}
