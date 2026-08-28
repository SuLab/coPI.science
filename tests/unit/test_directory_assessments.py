"""An incomplete-panel verdict must be visibly distinct from a vetted one.

Storing it (Task 3) is only safe if the page says so — otherwise Task 3 turns
a loud refusal into a silent, ordinary-looking row.
"""

import pytest

from src.models.opportunity import OpportunityAssessment
from src.services.assessment_detail import PANEL_STATES
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
    """A dimension whose max never rises is not discriminating, and at 15%
    weight that is worth seeing on a page that otherwise shows only totals."""
    run = await factories.make_simulation_run(db_session)
    for score in (1, 2, 2):
        db_session.add(
            OpportunityAssessment(
                simulation_run_id=run.id, agent_id="blackbird",
                subject_agent_id="wu", channel_name="general",
                band="pass",
                scores={"venture_potential": score, "differentiation_unmet_need": 5},
            )
        )
    await db_session.commit()

    view = await list_assessments(db_session, str(run.id))
    stats = {d["dimension"]: d for d in view["dimension_stats"]}

    assert stats["venture_potential"]["max"] == 2
    assert stats["differentiation_unmet_need"]["max"] == 5
    assert stats["venture_potential"]["specialist"] == "commercial", (
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


# ---------------------------------------------------------------------------
# The drift alarm: one definition of "unvetted", two representations
#
# `panel_state` is the Python-side state machine and `unvetted_panel_filter()`
# is its SQL twin — the banner cannot join to a Python function, so the rule
# necessarily exists twice. The first version of this change let the two drift
# by construction: `PANEL_STATES_UNVETTED` sat in `assessment_detail` claiming
# `directory.py` used it, while `directory.py` carried an independent, hand-
# written `or_(...)` and never referenced the constant. Adding a sixth state to
# the frozenset would have looked like it updated the banner and would have
# changed nothing at all — the banner would have kept under-warning on exactly
# the class of row the new state was invented to name.
#
# This walks the FULL matrix of the three columns the state machine reads and
# asserts the two representations agree row for row, not merely in total.
# ---------------------------------------------------------------------------

_PANEL_MATRIX = [
    (incomplete, domains, owed)
    for incomplete in (True, False)
    for domains in (None, [], ["chemistry"])
    for owed in (True, False, None)
]


@pytest.mark.asyncio
async def test_the_sql_unvetted_filter_matches_panel_state_row_for_row(db_session):
    """`unvetted_panel_filter()` selects a row IFF `panel_state(row)` is in
    `PANEL_STATES_UNVETTED` — over every combination of the three columns, not
    just the ones production happens to hold today."""
    from sqlalchemy import select

    from src.services.assessment_detail import (
        PANEL_STATES_UNVETTED,
        panel_state,
        unvetted_panel_filter,
    )

    run = await factories.make_simulation_run(db_session)
    for index, (incomplete, domains, owed) in enumerate(_PANEL_MATRIX):
        db_session.add(
            OpportunityAssessment(
                simulation_run_id=run.id, agent_id="blackbird",
                subject_agent_id="gordy", channel_name="general",
                # The combination, spelled into the row, so a mismatch names
                # itself instead of printing two sets of UUIDs.
                company_or_project=(
                    f"[{index}] incomplete={incomplete} "
                    f"domains={domains!r} owed={owed!r}"
                ),
                panel_incomplete=incomplete, missing_domains=domains,
                panel_owed=owed,
            )
        )
    await db_session.commit()
    run_id = run.id  # captured before expire_all() below expires `run` too

    # `expire_on_commit=False` (tests/conftest.py) means the objects `stored`
    # is about to fetch are still in the session's identity map, unexpired —
    # without this, the query below would just hand back the SAME Python
    # objects the loop above built, and `panel_state(row)` would read
    # attributes the TEST assigned, never anything Postgres actually stored.
    # `expire_all()` forces the next attribute access on every one of them to
    # re-SELECT, so `stored` below is a genuine read-back.
    db_session.expire_all()

    stored = (await db_session.execute(
        select(OpportunityAssessment).where(
            OpportunityAssessment.simulation_run_id == run_id
        )
    )).scalars().all()
    assert len(stored) == len(_PANEL_MATRIX) == 18

    # Computed from the rows as the DATABASE has them, so a storage-level
    # surprise (the JSONB `null`-vs-SQL-NULL trap `none_as_null` exists for)
    # cannot hide between the two sides of the comparison.
    expected = {
        row.company_or_project for row in stored
        if panel_state(row) in PANEL_STATES_UNVETTED
    }
    actual = set((await db_session.execute(
        select(OpportunityAssessment.company_or_project).where(
            OpportunityAssessment.simulation_run_id == run_id,
            unvetted_panel_filter(),
        )
    )).scalars().all())

    assert actual == expected
    # Non-degenerate: a filter that selected everything, or nothing, would agree
    # with a state machine that did the same and prove nothing about either.
    assert 0 < len(expected) < len(_PANEL_MATRIX)
    # And every state really is exercised, so "the matrix covers the machine" is
    # asserted rather than assumed.
    assert {panel_state(row) for row in stored} == set(PANEL_STATES)


@pytest.mark.asyncio
async def test_the_banner_count_is_the_same_predicate(db_session):
    """The count the page actually renders, over the same matrix. Binds the
    warning a reader sees to the filter above — not just the filter to the state
    machine."""
    from src.services.assessment_detail import PANEL_STATES_UNVETTED, panel_state

    run = await factories.make_simulation_run(db_session)
    for index, (incomplete, domains, owed) in enumerate(_PANEL_MATRIX):
        db_session.add(
            OpportunityAssessment(
                simulation_run_id=run.id, agent_id="blackbird",
                subject_agent_id="gordy", channel_name="general",
                company_or_project=f"row {index}",
                panel_incomplete=incomplete, missing_domains=domains,
                panel_owed=owed,
            )
        )
    await db_session.commit()

    view = await list_assessments(db_session, str(run.id))
    expected = sum(
        1 for row in view["assessments"]
        if panel_state(row) in PANEL_STATES_UNVETTED
    )
    assert view["incomplete_panel_count"] == expected
    assert 0 < expected < len(_PANEL_MATRIX)


@pytest.mark.asyncio
async def test_the_view_counts_stored_rows_per_run(db_session):
    """The run dropdown must distinguish an empty run from a populated one —
    'No assessments recorded yet.' used to be the same string for both."""
    run_a = await factories.make_simulation_run(db_session)
    run_b = await factories.make_simulation_run(db_session)
    for _ in range(2):
        db_session.add(OpportunityAssessment(
            simulation_run_id=run_a.id, agent_id="blackbird",
            channel_name="general", recommendation="pass",
        ))
    db_session.add(OpportunityAssessment(
        simulation_run_id=run_b.id, agent_id="blackbird",
        channel_name="general", recommendation="pass",
    ))
    await db_session.commit()

    view = await list_assessments(db_session, str(run_a.id))
    assert view["assessment_counts_by_run"][run_a.id] == 2
    assert view["assessment_counts_by_run"][run_b.id] == 1


@pytest.mark.asyncio
async def test_off_rubric_rows_are_counted_not_silently_dropped(db_session):
    """dimension_stats picks values by live key, so an archived-revision row
    contributes nothing — the page must SAY so instead of looking authoritative
    over a corpus it ignored."""
    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        recommendation="pass", rubric_version="2.1.0",
        rubric_content_hash="2f38fc9bce4d", scores={"ip_fto": 3},
    ))
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        recommendation="pass", scores={"differentiation_unmet_need": 4},
    ))
    await db_session.commit()

    view = await list_assessments(db_session, str(run.id))
    assert view["off_rubric_count"] == 1
