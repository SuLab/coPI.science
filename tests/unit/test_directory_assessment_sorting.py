"""Ordering and lab-filtering of the assessments triage queue.

Why these are service-level tests and not page tests: the ordering is done in
SQL under a LIMIT (``ASSESSMENTS_LIMIT``), so a sort that silently fell back to
the default, or one implemented in Python after the LIMIT, would still render a
page full of plausible rows in a plausible order. The only way to see the
difference is to seed rows whose four orders are PAIRWISE DISTINCT and assert
the whole sequence — which is what ``_seed`` below is built for.

``created_at`` is set explicitly on every row. Postgres' ``now()`` is
transaction-scoped, so rows inserted in one test transaction all share a
``created_at`` to the microsecond and ``sort=recent`` could not be told from
any other order at all.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.models.opportunity import OpportunityAssessment
from src.services.directory import (
    ASSESSMENT_SORT_OPTIONS,
    ASSESSMENT_SORTS,
    list_assessments,
)
from tests import factories

# (project, lab, score, recommendation, minutes-ago)
#
# Chosen so that score / recent / recommendation / lab each produce a different
# sequence, and so that every "hard" value is present: a NULL score, a NULL
# lab, a NULL recommendation, and `route-to-incubation` (which the computed
# band never produces and which must not sort by band).
_ROWS = (
    ("Alpha Co", "alpha", 4.5, "pass", 50),
    ("Bravo Co", "bravo", None, "advance", 10),
    ("Charlie Co", "charlie", 3.0, "conditional", 30),
    ("Delta Co", "delta", 1.0, "route-to-incubation", 20),
    ("Echo Co", None, 2.0, None, 40),
)


async def _seed(db_session):
    run = await factories.make_simulation_run(db_session)
    now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    for project, lab, score, recommendation, minutes in _ROWS:
        db_session.add(
            OpportunityAssessment(
                simulation_run_id=run.id,
                agent_id="blackbird",
                subject_agent_id=lab,
                channel_name="general",
                company_or_project=project,
                weighted_score=score,
                recommendation=recommendation,
                created_at=now - timedelta(minutes=minutes),
            )
        )
    await db_session.commit()
    return run


def _projects(view) -> list[str]:
    return [a.company_or_project for a in view["assessments"]]


@pytest.mark.asyncio
async def test_the_four_sorts_produce_four_different_orders(db_session):
    """One assertion block, on purpose: the value of these fixtures is that no
    two expected sequences are equal, so a sort that quietly fell through to
    the default fails here instead of passing on a coincidence."""
    run = await _seed(db_session)

    by_score = await list_assessments(db_session, str(run.id), sort="score")
    by_recent = await list_assessments(db_session, str(run.id), sort="recent")
    by_rec = await list_assessments(db_session, str(run.id), sort="recommendation")
    by_lab = await list_assessments(db_session, str(run.id), sort="lab")

    # Score desc, NULLS LAST — an unscored verdict must not float to the top of
    # a triage queue.
    assert _projects(by_score) == [
        "Alpha Co", "Charlie Co", "Echo Co", "Delta Co", "Bravo Co"
    ]
    # Newest first.
    assert _projects(by_recent) == [
        "Bravo Co", "Delta Co", "Charlie Co", "Echo Co", "Alpha Co"
    ]
    # Triage order of the model's own verdict, NOT band order (Delta's
    # route-to-incubation outranks Alpha's pass despite scoring 1.0 to 4.5),
    # and an absent recommendation lands with the unrecognized ones, last.
    assert _projects(by_rec) == [
        "Bravo Co", "Charlie Co", "Delta Co", "Alpha Co", "Echo Co"
    ]
    # Lab ascending, unattributed last.
    assert _projects(by_lab) == [
        "Alpha Co", "Bravo Co", "Charlie Co", "Delta Co", "Echo Co"
    ]

    # Non-vacuity: four distinct sequences from the same five rows.
    orders = {
        tuple(_projects(v)) for v in (by_score, by_recent, by_rec, by_lab)
    }
    assert len(orders) == 4


@pytest.mark.asyncio
async def test_score_is_the_tiebreak_within_a_recommendation_group(db_session):
    run = await factories.make_simulation_run(db_session)
    now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    for project, score in (("Low advance", 3.1), ("High advance", 4.9)):
        db_session.add(
            OpportunityAssessment(
                simulation_run_id=run.id, agent_id="blackbird",
                subject_agent_id="wang", channel_name="general",
                company_or_project=project, recommendation="advance",
                weighted_score=score, created_at=now,
            )
        )
    await db_session.commit()

    view = await list_assessments(db_session, str(run.id), sort="recommendation")
    assert _projects(view) == ["High advance", "Low advance"]


@pytest.mark.asyncio
async def test_recommendation_ranking_ignores_case_and_padding(db_session):
    """The value comes from the model's sidecar, so "Advance " is a realistic
    row. Ranking it with the unrecognized verdicts would bury a real advance
    under every decline."""
    run = await factories.make_simulation_run(db_session)
    now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="a",
            channel_name="general", company_or_project="Shouty advance",
            recommendation=" Advance ", weighted_score=1.0, created_at=now,
        )
    )
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="b",
            channel_name="general", company_or_project="Plain pass",
            recommendation="pass", weighted_score=4.9, created_at=now,
        )
    )
    await db_session.commit()

    view = await list_assessments(db_session, str(run.id), sort="recommendation")
    assert _projects(view) == ["Shouty advance", "Plain pass"]


@pytest.mark.asyncio
async def test_recent_orders_deterministically_when_timestamps_tie(db_session):
    """`created_at` is NOT unique — Postgres' now() is transaction-scoped, so a
    batch of verdicts written together really does share one timestamp. With
    `created_at DESC` as the only term, the database picks the order among the
    tied rows, so under the LIMIT a page can gain and lose a row between two
    renders of identical data. The tiebreak is `id DESC`; Postgres compares a
    uuid as 16 big-endian bytes and `uuid.UUID.__lt__` compares `.int`, so the
    expected order can be computed here rather than hardcoded.
    """
    run = await factories.make_simulation_run(db_session)
    tied = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    rows = [
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird",
            subject_agent_id="wang", channel_name="general",
            company_or_project=f"Tie {n}", created_at=tied,
        )
        for n in range(5)
    ]
    for row in rows:
        db_session.add(row)
    await db_session.commit()

    view = await list_assessments(db_session, str(run.id), sort="recent")

    expected = [
        row.company_or_project for row in sorted(rows, key=lambda r: r.id, reverse=True)
    ]
    assert _projects(view) == expected


@pytest.mark.asyncio
async def test_an_unknown_sort_falls_back_to_the_default_silently(db_session):
    run = await _seed(db_session)

    default = await list_assessments(db_session, str(run.id))
    bogus = await list_assessments(db_session, str(run.id), sort="by-vibes")

    assert default["sort"] == "score"
    assert bogus["sort"] == "score", "a stale bookmark must not select a phantom sort"
    assert _projects(bogus) == _projects(default)


@pytest.mark.asyncio
async def test_default_ordering_is_unchanged_by_the_new_parameters(db_session):
    """The pre-existing contract: score desc NULLS LAST, then most recent."""
    run = await _seed(db_session)
    view = await list_assessments(db_session, str(run.id))
    assert _projects(view) == [
        "Alpha Co", "Charlie Co", "Echo Co", "Delta Co", "Bravo Co"
    ]
    assert view["lab_filter"] is None
    assert view["sort_options"] == ASSESSMENT_SORT_OPTIONS


def test_every_offered_sort_is_an_accepted_sort():
    """The dropdown and the validator read the same tuple, which is the point:
    an option the service rejects would render as a control that does nothing."""
    assert tuple(value for value, _ in ASSESSMENT_SORT_OPTIONS) == ASSESSMENT_SORTS
    assert all(label for _, label in ASSESSMENT_SORT_OPTIONS)


@pytest.mark.asyncio
async def test_the_lab_filter_scopes_the_rows_and_the_total(db_session):
    run = await _seed(db_session)

    view = await list_assessments(db_session, str(run.id), lab="charlie")

    assert _projects(view) == ["Charlie Co"]
    assert view["total_count"] == 1, "the truncation note must count the filter"
    assert view["lab_filter"] == "charlie"
    # The dropdown still offers every other lab: options come from the run
    # scope, BEFORE the lab filter, or there would be no way back.
    assert view["lab_options"] == ["alpha", "bravo", "charlie", "delta"]


@pytest.mark.asyncio
async def test_an_unknown_lab_is_dropped_rather_than_emptying_the_page(db_session):
    run = await _seed(db_session)

    view = await list_assessments(db_session, str(run.id), lab="ghostlab")

    assert view["lab_filter"] is None
    assert view["total_count"] == len(_ROWS)
    assert len(view["assessments"]) == len(_ROWS)


@pytest.mark.asyncio
async def test_lab_options_are_scoped_to_the_selected_run(db_session):
    """With one run in the database a correctly-scoped option list and a
    completely unscoped one are identical, so this seeds a second run."""
    run = await _seed(db_session)
    other = await factories.make_simulation_run(db_session)
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=other.id, agent_id="blackbird",
            subject_agent_id="zulu", channel_name="general",
            company_or_project="Other run Co",
        )
    )
    await db_session.commit()

    scoped = await list_assessments(db_session, str(run.id))
    assert "zulu" not in scoped["lab_options"]

    everything = await list_assessments(db_session, "all")
    assert "zulu" in everything["lab_options"]
    assert "alpha" in everything["lab_options"]


@pytest.mark.asyncio
async def test_lab_options_survive_the_display_limit(db_session, monkeypatch):
    """The options must come from the query, not from the fetched rows: with a
    cap of 1, deriving them from `assessments` would offer exactly one lab and
    make every other one unreachable from the UI."""
    from src.services import directory as directory_service

    run = await _seed(db_session)
    monkeypatch.setattr(directory_service, "ASSESSMENTS_LIMIT", 1)

    view = await list_assessments(db_session, str(run.id))

    assert len(view["assessments"]) == 1
    assert view["total_count"] == len(_ROWS)
    assert view["lab_options"] == ["alpha", "bravo", "charlie", "delta"]


@pytest.mark.asyncio
async def test_the_lab_filter_does_not_narrow_the_incomplete_panel_warning(db_session):
    """A warning that quietly shrinks to the current filter is worse than one
    that over-reports: the unvetted verdict is the RUN's problem, and the
    reader who filtered to another lab still has to be told."""
    run = await factories.make_simulation_run(db_session)
    db_session.add(
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="gordy",
            channel_name="general", company_or_project="Gapped Co",
            panel_incomplete=True, missing_domains=["chemistry"], panel_owed=True,
        )
    )
    db_session.add(
        # "Clean" now has to be SAID. The warning counts every row whose panel
        # is not verified complete, and a row that leaves `panel_owed` at its
        # default is `NULL` — "we do not know whether any floor ran" — which is
        # one of the three states it counts. Leaving it unset would make this
        # test about the widened count instead of about the lab filter.
        OpportunityAssessment(
            simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
            channel_name="general", company_or_project="Clean Co",
            panel_incomplete=False, missing_domains=None, panel_owed=True,
        )
    )
    await db_session.commit()

    view = await list_assessments(db_session, str(run.id), lab="wang")
    assert _projects(view) == ["Clean Co"]
    assert view["incomplete_panel_count"] == 1
