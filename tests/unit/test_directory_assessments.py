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


# ---------------------------------------------------------------------------
# Task E: per-row dimension_rows / revision_view (C4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dimension_rows_use_the_archived_revision_stamped_on_the_row(db_session):
    """A row stamped with an archived revision's (version, content_hash) gets
    THAT revision's titles and weights, not the live document's — the same
    revision `build_assessment_detail` would resolve for it."""
    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        recommendation="pass", rubric_version="2.1.0",
        rubric_content_hash="2f38fc9bce4d", scores={"ip_fto": 3},
    ))
    await db_session.commit()

    view = await list_assessments(db_session, str(run.id))
    row = view["assessments"][0]
    assert row.revision_view is not None
    assert row.revision_view.version == "2.1.0"
    # Every dimension the ARCHIVED revision names renders (unscored ones with
    # score=None, pct=0.0 — a known revision means a known scale, so an
    # unscored dimension still gets a zero-width bar rather than an unknown
    # one), and the one the row actually scored carries its value.
    by_key = {d["key"]: d for d in row.dimension_rows}
    assert set(by_key) == {
        "differentiation", "market_unmet_need", "team", "external_signals",
        "ip_fto", "platform", "dev_regulatory_feasibility",
        "workplan_capital_efficiency", "exit_thesis", "mechanism_validation",
        "toxicity_selectivity", "experimental_rigor", "chemistry_dc_path",
    }
    assert by_key["ip_fto"] == {
        "key": "ip_fto",
        "title": "IP position & FTO",
        "score": 3.0,
        "weight": 6,
        "weight_note": "6%/4% (investment/incubation)",
        "pct": 60.0,
    }
    assert by_key["team"]["score"] is None
    assert by_key["team"]["pct"] == 0.0


@pytest.mark.asyncio
async def test_dimension_rows_use_the_live_revision_when_unstamped(db_session):
    """An unstamped row (no rubric_version/rubric_content_hash) is read
    against the LIVE document, exactly like the detail page's unstamped
    fallback."""
    from src.services.blackbird_rubric import RUBRIC_VERSION

    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        recommendation="pass", scores={"differentiation_unmet_need": 4},
    ))
    await db_session.commit()

    view = await list_assessments(db_session, str(run.id))
    row = view["assessments"][0]
    assert row.revision_view is not None
    assert row.revision_view.version == RUBRIC_VERSION
    by_key = {d["key"]: d for d in row.dimension_rows}
    assert set(by_key) == {
        "differentiation_unmet_need", "scientific_credibility",
        "translational_path", "fundable_experiment", "venture_potential",
        "team_executability",
    }
    assert by_key["differentiation_unmet_need"] == {
        "key": "differentiation_unmet_need",
        "title": "Differentiation & unmet need",
        "score": 4.0,
        "weight": 25,
        "weight_note": "25%",
        "pct": 80.0,
    }
    assert by_key["team_executability"]["score"] is None
    assert by_key["team_executability"]["pct"] == 0.0


@pytest.mark.asyncio
async def test_dimension_rows_still_render_every_stored_key_when_all_are_off_rubric(db_session):
    """A row whose `scores` keys are all off-rubric must still yield one entry
    per stored key — untitled/unweighted rather than dropped (the
    pre-registry-page bug this whole change exists to avoid reintroducing)."""
    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        recommendation="pass", scores={"foo_bar": 3, "baz_qux": 2},
    ))
    await db_session.commit()

    view = await list_assessments(db_session, str(run.id))
    row = view["assessments"][0]
    by_key = {d["key"]: d for d in row.dimension_rows}
    # Unstamped, so resolves to the live revision — its six named dimensions
    # still render (unscored, score=None), PLUS one entry per off-rubric
    # stored key. Neither the pre-registry "drop what the revision doesn't
    # name" bug nor a silent loss of the live dimensions is acceptable.
    assert {"foo_bar", "baz_qux"} <= set(by_key)
    assert by_key["foo_bar"]["title"] == "foo bar"
    assert by_key["foo_bar"]["weight"] is None
    assert by_key["foo_bar"]["weight_note"] is None
    assert by_key["foo_bar"]["pct"] == 60.0
    assert by_key["baz_qux"]["pct"] == 40.0


@pytest.mark.asyncio
async def test_a_row_with_no_scores_still_gets_the_revisions_dimensions(db_session):
    """SUPERSEDES `..._are_empty_for_a_row_with_no_scores` (2026-09-14 audit).
    `scores=None` is the ordinary state for an unscored row, and it used to
    short-circuit to `([], None)` here — which made the card say "no
    per-dimension scores were stored" while the detail page rendered the
    revision's six dimensions as "not scored". Same verdict, two answers. An
    unstamped row resolves to the LIVE revision, so it has rows; `([], None)`
    is now reached only when there is nothing to say on either surface."""
    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        recommendation="pass",
    ))
    await db_session.commit()

    view = await list_assessments(db_session, str(run.id))
    row = view["assessments"][0]
    assert row.revision_view is not None
    assert row.dimension_rows, "the live revision names dimensions to render"
    assert all(d["score"] is None for d in row.dimension_rows)


@pytest.mark.asyncio
async def test_the_view_returns_no_new_top_level_context_key(db_session):
    """Guards P4 directly: `dimension_rows`/`revision_view` ride on each row in
    `assessments`, never as a new top-level key — a new key would reach the
    admin template (which allowlists every key it forwards,
    `src/routers/admin.py`) or the manager template (which splats the whole
    view) but not both."""
    run = await factories.make_simulation_run(db_session)
    db_session.add(OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", channel_name="general",
        recommendation="pass",
    ))
    await db_session.commit()

    view = await list_assessments(db_session, str(run.id))
    assert set(view.keys()) == {
        "assessments",
        "banding",
        "rubric_version",
        "runs",
        "runs_by_id",
        "selected_run_id",
        "show_all_runs",
        "sort",
        "sort_options",
        "lab_filter",
        "lab_options",
        "pi_user_ids",
        "total_count",
        "assessments_limit",
        "drop_counts",
        "drops_total",
        "incomplete_panel_count",
        "dimension_stats",
        "band_counts",
        "assessment_counts_by_run",
        "off_rubric_count",
    }


async def test_a_row_with_no_scores_yields_the_same_rows_as_the_detail_page(db_session):
    """2026-09-14 audit. `_assessment_dimension_rows` used to short-circuit to
    `([], None)` for a row with no `scores`, so the CARD said "no per-dimension
    scores were stored" while the detail page one click away rendered the
    revision's six dimensions as "not scored" — the same verdict with two
    answers, which is the drift the mirroring exists to prevent."""
    from src.services.assessment_detail import build_assessment_detail
    from src.services.directory import _assessment_dimension_rows

    run = await factories.make_simulation_run(db_session)
    assessment = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="general", company_or_project="No scores at all", scores=None,
    )
    db_session.add(assessment)
    await db_session.flush()

    card_rows, card_revision = _assessment_dimension_rows(assessment)
    detail = await build_assessment_detail(db_session, assessment.id, admin_view=True)

    assert [r["key"] for r in card_rows] == [d["key"] for d in detail["dimensions"]]
    assert [r["score"] for r in card_rows] == [d["score"] for d in detail["dimensions"]]
    assert [r["pct"] for r in card_rows] == [d["pct"] for d in detail["dimensions"]]
    assert card_rows, "an unstamped row resolves to the live revision, so it has rows"
    assert card_revision is not None

