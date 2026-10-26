"""The projects measures report (``src.services.projects_report``) against a hand-built run.

One lab with three projects at a 24-hour interval: ready (requested at 0 h, finished
at 60 h), not agreed (84 h → 96 h) and not finished on a time-out (144 h → 156 h), with
the run clock at 168 h. Between the first two requests two whole intervals pass while
the first project is still open; between the second and the third one passes after the
second finished. Every figure below is computed by hand from those numbers.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from scripts import projects_report as script
from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_REVIEWER,
    AssessmentReview,
    HubProject,
    HubProjectCall,
    HubProjectClock,
    HubProjectReview,
    OpportunityAssessment,
)
from src.models.hub_project import (
    CALL_DONE,
    REASON_THREAD_TIMEOUT,
    STATUS_NOT_AGREED,
    STATUS_NOT_FINISHED,
    STATUS_POSTED,
    STATUS_READY,
)
from src.services.projects_report import (
    MISSED_NOT_LIVE,
    MISSED_PREVIOUS_OPEN,
    build_report,
    refinement_cost,
)
from src.services.projects_view import build_projects_list
from tests import factories

pytestmark = pytest.mark.integration

LAB = "tstreportlab"
OTHER_LAB = "tstreportother"
HUB = "blackbird"
DAY = 86400.0
PRICED = "claude-opus-5"  # $5 / MTok input, $25 / MTok output
READY_TS = "1820000000.000100"
NOW = datetime.now(UTC)


def _usage(inp: int, out: int) -> list[dict]:
    return [{
        "model": PRICED, "input_tokens": inp, "output_tokens": out,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }]


def _project(run, slot, status, requested_days, finished_days, **extra):
    return HubProject(
        simulation_run_id=run.id, agent_id=LAB, slot=slot, status=status,
        requested_run_seconds=requested_days * DAY,
        finished_run_seconds=None if finished_days is None else finished_days * DAY,
        **extra,
    )


async def _call(db_session, run, *, at, agent_id=HUB, phase="project_thread", thread_ts=None):
    """One call of 1,000 input and 100 output tokens: $0.0075."""
    return await factories.make_llm_call_log(
        db_session, run=run, agent_id=agent_id, phase=phase, thread_ts=thread_ts,
        model=PRICED, input_tokens=1000, output_tokens=100,
        cache_read_input_tokens=0, cache_creation_input_tokens=0, created_at=at,
    )


async def _seed(db_session):
    run = await factories.make_simulation_run(db_session, status="stopped")
    db_session.add(HubProjectClock(simulation_run_id=run.id, run_seconds=7 * DAY))
    ready = _project(
        run, 1, STATUS_READY, 0, 2.5, title="Ready project", reply_ts=READY_TS,
        posted_at=NOW - timedelta(hours=1), finished_at=NOW + timedelta(hours=1),
        choice={
            "anchors": [
                {"section": "Key Methods and Technologies", "item": "Cryo-EM"},
                {"section": "Keywords", "item": "kinases"},
            ],
            "sources": [
                {"key": "PMID:31500698", "kind": "pubmed", "title": "A"},
                {"key": "NCT01903460", "kind": "trial", "title": "B"},
            ],
        },
        final={"gaps": ["Design is not filled"], "disagreements": []},
    )
    not_agreed = _project(
        run, 2, STATUS_NOT_AGREED, 3.5, 4, title="Not agreed project",
        choice={
            "anchors": [{"section": "Key Methods and Technologies", "item": "Mass spec"}],
            "sources": [{"key": "US1234567", "kind": "patent", "title": "C"}],
        },
        final={"gaps": ["Design is not filled", "Budget unclear"], "disagreements": ["Scope"]},
    )
    timed_out = _project(
        run, 3, STATUS_NOT_FINISHED, 6, 6.5, title="Timed-out project",
        reason_code=REASON_THREAD_TIMEOUT,
    )
    db_session.add_all([ready, not_agreed, timed_out])
    await db_session.flush()

    # Research ledger: $0.10 for the ready project, $0.20 for the not agreed one.
    db_session.add_all([
        HubProjectCall(project_id=ready.id, step="research:scientific", model=PRICED,
                       status=CALL_DONE, usage_by_model=_usage(10_000, 2_000)),
        HubProjectCall(project_id=not_agreed.id, step="research:scientific", model=PRICED,
                       status=CALL_DONE, usage_by_model=_usage(10_000, 2_000)),
        HubProjectCall(project_id=not_agreed.id, step="choose", model=PRICED,
                       status=CALL_DONE, usage_by_model=_usage(10_000, 2_000)),
    ])
    # Refinement of the ready project: two thread calls and the lab's reply step in its
    # window ($0.0225). Not counted: a reply step before the posting, another lab's reply
    # step, and a call on the same thread id in another run.
    inside = NOW - timedelta(minutes=30)
    await _call(db_session, run, at=inside, thread_ts=READY_TS)
    await _call(db_session, run, at=inside, agent_id=LAB, thread_ts=READY_TS)
    await _call(db_session, run, at=inside, agent_id=LAB, phase="project_reply")
    await _call(db_session, run, at=NOW - timedelta(days=1), agent_id=LAB, phase="project_reply")
    await _call(db_session, run, at=inside, agent_id=OTHER_LAB, phase="project_reply")
    other_run = await factories.make_simulation_run(db_session)
    await _call(db_session, other_run, at=inside, thread_ts=READY_TS)

    # Two project reviews (4 fund, 3 don't), and two assessment reviews (2, 4).
    reviewers = [
        await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER) for _ in range(2)
    ]
    db_session.add_all([
        HubProjectReview(project_id=ready.id, reviewer_user_id=reviewers[0].id,
                         reviewer_name=reviewers[0].name, rating=4, fund=True),
        HubProjectReview(project_id=ready.id, reviewer_user_id=reviewers[1].id,
                         reviewer_name=reviewers[1].name, rating=3, fund=False),
    ])
    assessment = OpportunityAssessment(
        simulation_run_id=run.id, agent_id=HUB, subject_agent_id=LAB,
        channel_name="scout-report", recommendation="advance",
    )
    db_session.add(assessment)
    await db_session.flush()
    db_session.add_all([
        AssessmentReview(assessment_id=assessment.id, reviewer_name="R", score=s,
                         feedback_mode="log_only")
        for s in (2, 4)
    ])
    await db_session.flush()
    return run, ready, not_agreed, timed_out


async def test_the_report_matches_hand_computed_figures(db_session):
    run, ready, not_agreed, timed_out = await _seed(db_session)

    report = await build_report(db_session, run.id)

    assert report["project_count"] == 3
    assert report["outcome_counts"] == {"ready": 1, "not_agreed": 1, "not_finished": 1}
    assert report["outcome_shares"] == pytest.approx(
        {"ready": 1 / 3, "not_agreed": 1 / 3, "not_finished": 1 / 3}
    )
    assert [(r["code"], r["count"]) for r in report["not_finished_reasons"]] == [
        (REASON_THREAD_TIMEOUT, 1)
    ]

    rows = {r["id"]: r for r in report["projects"]}
    assert rows[str(ready.id)]["research_usd"] == Decimal("0.10")
    assert rows[str(not_agreed.id)]["research_usd"] == Decimal("0.20")
    assert rows[str(timed_out.id)]["research_usd"] == Decimal(0)
    assert rows[str(ready.id)]["refinement_usd"] == Decimal("0.0225")
    assert rows[str(not_agreed.id)]["refinement_usd"] == Decimal(0)
    assert await refinement_cost(db_session, ready) == Decimal("0.0225")
    assert report["cost"]["research_usd"] == Decimal("0.30")
    assert report["cost"]["refinement_usd"] == Decimal("0.0225")
    assert report["cost"]["total_usd"] == Decimal("0.3225")
    assert report["cost"]["unpriced_models"] == []

    assert report["top_gaps"] == [
        {"text": "Design is not filled", "count": 2}, {"text": "Budget unclear", "count": 1}
    ]
    assert report["top_disagreements"] == [{"text": "Scope", "count": 1}]
    assert report["anchor_sections"] == {"Key Methods and Technologies": 2, "Keywords": 1}
    assert report["sources_by_kind"] == {"pubmed": 1, "trial": 1, "patent": 1}
    assert report["sources_per_project"] == pytest.approx(1.5)

    (lab,) = report["labs"]
    assert lab["agent_id"] == LAB and lab["requested"] == 3
    assert lab["missed_by_reason"] == {MISSED_PREVIOUS_OPEN: 2, MISSED_NOT_LIVE: 1}
    assert [(m["from_hours"], m["reason"]) for m in lab["missed_intervals"]] == [
        (24.0, MISSED_PREVIOUS_OPEN), (48.0, MISSED_PREVIOUS_OPEN), (108.0, MISSED_NOT_LIVE),
    ]
    assert lab["per_run_day"] == pytest.approx(3 / 7)
    assert report["missed_by_reason"] == {MISSED_PREVIOUS_OPEN: 2, MISSED_NOT_LIVE: 1}

    reviews = report["reviews"]
    assert reviews["project_review_count"] == 2
    assert reviews["project_mean_rating"] == pytest.approx(3.5)
    assert reviews["project_fund_share"] == pytest.approx(0.5)
    assert reviews["assessment_review_count"] == 2
    assert reviews["assessment_mean_score"] == pytest.approx(3.0)

    assert report["hours_to_finish"] == {
        "count": 3, "mean": pytest.approx(28.0), "median": pytest.approx(12.0)
    }

    markdown = script.render_markdown(report)
    assert MISSED_PREVIOUS_OPEN in markdown and "$0.32" in markdown


async def test_the_list_summary_agrees_with_the_report(db_session):
    run, *_ = await _seed(db_session)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)

    report = await build_report(db_session, run.id)
    view = await build_projects_list(
        db_session, viewer=admin, impersonating=False, run_id=str(run.id),
        lab=None, outcome=None, mine=False,
    )

    summary = view["summary"]
    assert summary["counts"] == report["outcome_counts"]
    assert summary["review_count"] == report["reviews"]["project_review_count"]
    assert summary["mean_rating"] == pytest.approx(report["reviews"]["project_mean_rating"])
    assert summary["fund_share"] == pytest.approx(report["reviews"]["project_fund_share"])


async def test_an_open_project_of_a_finalized_run_counts_as_not_finished(db_session):
    run = await factories.make_simulation_run(db_session, status="stopped", finalized_at=NOW)
    db_session.add(HubProjectClock(simulation_run_id=run.id, run_seconds=DAY / 2))
    db_session.add(_project(run, 1, STATUS_POSTED, 0, None, posted_at=NOW))
    await db_session.flush()

    report = await build_report(db_session, run.id)

    assert report["outcome_counts"] == {"not_finished": 1}
    assert [(r["code"], r["count"]) for r in report["not_finished_reasons"]] == [
        ("run_finalized", 1)
    ]
    assert report["labs"][0]["missed_intervals"] == []


async def test_an_unknown_run_raises_lookup_error(db_session):
    with pytest.raises(LookupError):
        await build_report(db_session, uuid.uuid4())


def test_the_script_refuses_an_out_path_inside_the_repository(tmp_path):
    inside = script.REPO_ROOT / "projects-report-should-not-exist.md"

    assert script.main(["--run", str(uuid.uuid4()), "--out", str(inside)]) == 2
    assert not inside.exists()
    assert script.out_path_allowed(tmp_path / "report.md")
    assert not script.out_path_allowed(script.REPO_ROOT / "docs" / "report.md")
