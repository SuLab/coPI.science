"""Project reviews: submit, edit and delete through /reviews/..., one per person, refused
under impersonation, kept apart from assessment reviews and kept with the author's name."""

import asyncio
import itertools
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import AssessmentReview, SimulationRun, User
from src.models.hub_project import HubProject, HubProjectReview
from src.services.project_reviews import DuplicateProjectReviewError, submit_project_review
from src.services.user_deletion import delete_user_account
from src.web.flash import FLASH_SESSION_KEY
from tests import factories
from tests.integration.test_manager_access import auth_headers
from tests.session_support import session_from_response

pytestmark = pytest.mark.integration

_SLOTS = itertools.count(1)
GOOD = {"rating": "4", "comment": "Worth a pilot.", "fund": "fund"}


async def _project(db) -> HubProject:
    run = await factories.make_simulation_run(db)
    project = HubProject(
        simulation_run_id=run.id, agent_id="lab1", slot=next(_SLOTS), status="ready",
        requested_run_seconds=0.0, title="A project", finished_at=datetime.now(UTC),
    )
    db.add(project)
    await db.flush()
    return project


async def _reviews(db, project_id) -> list[HubProjectReview]:
    return list(
        (
            await db.execute(
                select(HubProjectReview)
                .where(HubProjectReview.project_id == project_id)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


def _last_flash(response) -> dict:
    return (session_from_response(response) or {})[FLASH_SESSION_KEY][-1]


def _redirects_to_reviews(response, project_id) -> bool:
    return (
        response.status_code == 303
        and response.headers["location"] == f"/workspace/projects/{project_id}#reviews"
    )


async def test_submit_edit_delete_cycle(client, db_session):
    project = await _project(db_session)
    reviewer = await factories.make_user(db_session, user_role="reviewer")
    manager = await factories.make_user(db_session, user_role="manager")
    admin = await factories.make_user(db_session, user_role="admin")
    submit_path = f"/reviews/projects/{project.id}/review"

    response = await client.post(submit_path, data=GOOD, headers=auth_headers(reviewer.id))
    assert _redirects_to_reviews(response, project.id)
    assert _last_flash(response) == {"text": "Review saved.", "kind": "success"}
    (review,) = await _reviews(db_session, project.id)
    assert (review.rating, review.fund, review.reviewer_name) == (4, True, reviewer.name)

    edit_path = f"/reviews/project-reviews/{review.id}/edit"
    edited = {"rating": "2", "comment": "On reflection, no.", "fund": "no_fund"}
    response = await client.post(edit_path, data=edited, headers=auth_headers(reviewer.id))
    assert _redirects_to_reviews(response, project.id)
    assert _last_flash(response) == {"text": "Review updated.", "kind": "success"}
    (review,) = await _reviews(db_session, project.id)
    assert (review.rating, review.comment, review.fund) == (2, "On reflection, no.", False)

    response = await client.post(submit_path, data=GOOD, headers=auth_headers(reviewer.id))
    assert _redirects_to_reviews(response, project.id)
    assert _last_flash(response) == {
        "text": "You have already reviewed this project; edit your review below.",
        "kind": "info",
    }
    assert len(await _reviews(db_session, project.id)) == 1

    response = await client.post(edit_path, data=GOOD, headers=auth_headers(manager.id))
    assert response.status_code == 403

    delete_path = f"/reviews/project-reviews/{review.id}/delete"
    response = await client.post(delete_path, headers=auth_headers(manager.id))
    assert response.status_code == 403
    response = await client.post(delete_path, headers=auth_headers(admin.id))
    assert _redirects_to_reviews(response, project.id)
    assert _last_flash(response) == {"text": "Review deleted.", "kind": "success"}
    assert await _reviews(db_session, project.id) == []


async def test_concurrent_double_submit_stores_one_row(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        run = SimulationRun(status="running", config={})
        s.add(run)
        await s.flush()
        project = HubProject(simulation_run_id=run.id, agent_id="lab1", slot=1, status="ready",
                             requested_run_seconds=0.0)
        reviewer = await factories.make_user(s, user_role="reviewer")
        s.add(project)
        await s.commit()

    async def submit():
        async with factory() as s:
            mine = await s.get(HubProject, project.id)
            who = await s.get(User, reviewer.id)
            try:
                await submit_project_review(s, project=mine, reviewer=who, rating=3,
                                            comment="", fund=False)
                await asyncio.sleep(0.2)  # hold the uncommitted row while the other inserts
                await s.commit()
                return "saved"
            except DuplicateProjectReviewError:
                await s.rollback()
                return "duplicate"

    try:
        assert sorted(await asyncio.gather(submit(), submit())) == ["duplicate", "saved"]
        async with factory() as s:
            count = await s.scalar(
                select(func.count()).where(HubProjectReview.project_id == project.id)
            )
            assert count == 1
    finally:
        async with factory() as s:
            await s.execute(delete(SimulationRun).where(SimulationRun.id == run.id))
            await s.execute(delete(User).where(User.id == reviewer.id))
            await s.commit()


async def test_rating_and_fund_validation(client, db_session):
    project = await _project(db_session)
    reviewer = await factories.make_user(db_session, user_role="reviewer")
    headers = auth_headers(reviewer.id)
    path = f"/reviews/projects/{project.id}/review"
    for bad in (
        {**GOOD, "rating": "0"},
        {**GOOD, "rating": "6"},
        {**GOOD, "fund": "maybe"},
        {**GOOD, "comment": "x" * 10_001},
    ):
        response = await client.post(path, data=bad, headers=headers)
        assert response.status_code == 400, bad
    assert await _reviews(db_session, project.id) == []


async def test_impersonated_writes_refused(client, db_session):
    project = await _project(db_session)
    admin = await factories.make_user(db_session, user_role="admin")
    manager = await factories.make_user(db_session, user_role="manager")
    review = HubProjectReview(project_id=project.id, reviewer_user_id=manager.id,
                              reviewer_name=manager.name, rating=3, fund=True)
    db_session.add(review)
    await db_session.flush()
    headers = auth_headers(admin.id, impersonate=manager.id)
    for path, data in (
        (f"/reviews/projects/{project.id}/review", GOOD),
        (f"/reviews/project-reviews/{review.id}/edit", GOOD),
        (f"/reviews/project-reviews/{review.id}/delete", {}),
    ):
        response = await client.post(path, data=data, headers=headers)
        assert response.status_code == 403, path
    (kept,) = await _reviews(db_session, project.id)
    assert kept.rating == 3


async def test_reviews_are_not_assessment_reviews(client, db_session):
    project = await _project(db_session)
    reviewer = await factories.make_user(db_session, user_role="reviewer")
    before = await db_session.scalar(select(func.count()).select_from(AssessmentReview))
    response = await client.post(
        f"/reviews/projects/{project.id}/review", data=GOOD, headers=auth_headers(reviewer.id)
    )
    assert response.status_code == 303
    assert len(await _reviews(db_session, project.id)) == 1
    after = await db_session.scalar(select(func.count()).select_from(AssessmentReview))
    assert after == before


async def test_deleted_reviewer_keeps_the_name(client, db_session):
    project = await _project(db_session)
    reviewer = await factories.make_user(db_session, user_role="reviewer", name="Departed Reviewer")
    await submit_project_review(db_session, project=project, reviewer=reviewer, rating=5,
                                comment="Fund it.", fund=True)
    await delete_user_account(db_session, reviewer)
    (review,) = await _reviews(db_session, project.id)
    assert review.reviewer_user_id is None
    assert review.reviewer_name == "Departed Reviewer"
    viewer = await factories.make_user(db_session, user_role="manager")
    html = (
        await client.get(f"/workspace/projects/{project.id}", headers=auth_headers(viewer.id))
    ).text
    assert "Departed Reviewer" in html
