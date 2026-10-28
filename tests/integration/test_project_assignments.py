"""Project review assignments (spec §7, plan contract C7): the service and the
``/reviews/projects/{id}/assign`` and ``/unassign`` routes. Staff only, refused under
impersonation, the assignee validated as the assessments' are, idempotent, and removed with
the run or the assignee."""

import itertools
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from src.models import SimulationRun
from src.models.hub_project import HubProject, HubProjectReviewAssignment
from src.services.project_reviews import (
    assign_project_reviewer,
    project_assignments,
    unassign_project_reviewer,
)
from src.services.user_deletion import delete_user_account
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

_SLOTS = itertools.count(1)


async def _project(db) -> HubProject:
    run = await factories.make_simulation_run(db)
    project = HubProject(
        simulation_run_id=run.id, agent_id="lab1", slot=next(_SLOTS), status="ready",
        requested_run_seconds=0.0, title="A project", finished_at=datetime.now(UTC),
    )
    db.add(project)
    await db.flush()
    return project


async def _rows(db, project_id) -> list[HubProjectReviewAssignment]:
    return list(
        (
            await db.execute(
                select(HubProjectReviewAssignment)
                .where(HubProjectReviewAssignment.project_id == project_id)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


async def test_staff_assign_and_unassign(client, db_session):
    project = await _project(db_session)
    manager = await factories.make_user(db_session, user_role="manager")
    reviewer = await factories.make_user(db_session, user_role="reviewer")
    headers = auth_headers(manager.id)
    form = {
        "assignee_user_id": str(reviewer.id),
        "surface": "project-detail",
        "lab": "lab1",
        "review": "all",
    }
    expected = f"/workspace/projects/{project.id}?lab=lab1&review=all#reviews"

    for _ in range(2):
        response = await client.post(
            f"/reviews/projects/{project.id}/assign", data=form, headers=headers
        )
        assert (response.status_code, response.headers["location"]) == (303, expected)
        (row,) = await _rows(db_session, project.id)
        assert (row.assignee_user_id, row.assignee_name) == (reviewer.id, reviewer.name)
        assert (row.assigned_by_user_id, row.assigned_by_name) == (manager.id, manager.name)

    for _ in range(2):
        response = await client.post(
            f"/reviews/projects/{project.id}/unassign", data=form, headers=headers
        )
        assert (response.status_code, response.headers["location"]) == (303, expected)
        assert await _rows(db_session, project.id) == []


async def test_reviewer_cannot_assign(client, db_session):
    project = await _project(db_session)
    reviewer = await factories.make_user(db_session, user_role="reviewer")
    for path in ("assign", "unassign"):
        response = await client.post(
            f"/reviews/projects/{project.id}/{path}",
            data={"assignee_user_id": str(reviewer.id)},
            headers=auth_headers(reviewer.id),
        )
        assert response.status_code == 403, path
    assert await _rows(db_session, project.id) == []


async def test_impersonated_assign_refused(client, db_session):
    project = await _project(db_session)
    admin = await factories.make_user(db_session, user_role="admin")
    manager = await factories.make_user(db_session, user_role="manager")
    reviewer = await factories.make_user(db_session, user_role="reviewer")
    await assign_project_reviewer(db_session, project=project, assignee=reviewer,
                                  assigned_by=admin)
    headers = auth_headers(admin.id, impersonate=manager.id)
    for path in ("assign", "unassign"):
        response = await client.post(
            f"/reviews/projects/{project.id}/{path}",
            data={"assignee_user_id": str(reviewer.id)},
            headers=headers,
        )
        assert response.status_code == 403, path
    assert len(await _rows(db_session, project.id)) == 1


async def test_assignee_must_be_allowed_staff_or_reviewer(client, db_session):
    project = await _project(db_session)
    manager = await factories.make_user(db_session, user_role="manager")
    pi = await factories.make_user(db_session, user_role="pi")
    denied = await factories.make_user(db_session, user_role="reviewer", access_status="denied")
    headers = auth_headers(manager.id)
    for assignee_id in (str(pi.id), str(denied.id), "not-a-uuid", str(uuid.uuid4())):
        response = await client.post(
            f"/reviews/projects/{project.id}/assign",
            data={"assignee_user_id": assignee_id},
            headers=headers,
        )
        assert response.status_code == 400, assignee_id
    assert await _rows(db_session, project.id) == []


async def test_unknown_project_is_404(client, db_session):
    manager = await factories.make_user(db_session, user_role="manager")
    reviewer = await factories.make_user(db_session, user_role="reviewer")
    response = await client.post(
        "/reviews/projects/00000000-0000-0000-0000-000000000000/assign",
        data={"assignee_user_id": str(reviewer.id)},
        headers=auth_headers(manager.id),
    )
    assert response.status_code == 404


async def test_project_assignments_oldest_first(db_session):
    project = await _project(db_session)
    other = await _project(db_session)
    manager = await factories.make_user(db_session, user_role="manager")
    first = await factories.make_user(db_session, user_role="reviewer")
    second = await factories.make_user(db_session, user_role="manager")
    await assign_project_reviewer(db_session, project=project, assignee=first,
                                  assigned_by=manager)
    await db_session.execute(
        HubProjectReviewAssignment.__table__.update()
        .where(HubProjectReviewAssignment.assignee_user_id == first.id)
        .values(created_at=datetime(2026, 1, 1, tzinfo=UTC))
    )
    await assign_project_reviewer(db_session, project=project, assignee=second,
                                  assigned_by=manager)
    result = await project_assignments(db_session, [project.id, other.id])
    assert list(result) == [project.id]
    assert [a.assignee_user_id for a in result[project.id]] == [first.id, second.id]
    assert await project_assignments(db_session, []) == {}
    await unassign_project_reviewer(db_session, project=project, assignee_user_id=first.id)
    result = await project_assignments(db_session, [project.id])
    assert [a.assignee_user_id for a in result[project.id]] == [second.id]


async def test_run_deletion_cascades(db_session):
    project = await _project(db_session)
    manager = await factories.make_user(db_session, user_role="manager")
    reviewer = await factories.make_user(db_session, user_role="reviewer")
    await assign_project_reviewer(db_session, project=project, assignee=reviewer,
                                  assigned_by=manager)
    project_id, run_id = project.id, project.simulation_run_id
    db_session.expunge(project)
    await db_session.execute(delete(SimulationRun).where(SimulationRun.id == run_id))
    assert await _rows(db_session, project_id) == []


async def test_user_deletion_cascades(db_session):
    project = await _project(db_session)
    manager = await factories.make_user(db_session, user_role="manager")
    reviewer = await factories.make_user(db_session, user_role="reviewer")
    await assign_project_reviewer(db_session, project=project, assignee=reviewer,
                                  assigned_by=manager)
    await delete_user_account(db_session, reviewer)
    assert await _rows(db_session, project.id) == []


async def test_assigner_deletion_keeps_the_row(db_session):
    project = await _project(db_session)
    manager = await factories.make_user(db_session, user_role="manager", name="Gone Manager")
    reviewer = await factories.make_user(db_session, user_role="reviewer")
    await assign_project_reviewer(db_session, project=project, assignee=reviewer,
                                  assigned_by=manager)
    await delete_user_account(db_session, manager)
    (row,) = await _rows(db_session, project.id)
    assert (row.assigned_by_user_id, row.assigned_by_name) == (None, "Gone Manager")
