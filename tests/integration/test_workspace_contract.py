"""Executable canonical/compatibility contracts and negative authorization controls."""
import uuid

import pytest
from sqlalchemy import select

from src.models import AssessmentReviewAssignment, OpportunityAssessment, User
from src.routers import manager, workspace
from src.services.directory import MAX_PAGE, list_assessments
from tests import factories
from tests.integration.test_manager_access import auth_headers
from tests.integration.test_pi_only_writes import _snapshot
from tests.workspace_support import POST_SUFFIXES

pytestmark = pytest.mark.integration



def _routes(router):
    for route in router.routes:
        ctx = getattr(route, 'include_context', None)
        if ctx:
            yield from _routes(ctx.included_router)
        else:
            yield route


def test_all_28_alias_writes_use_identical_callable_and_staff_dependency():
    canonical = {r.path: r for r in _routes(workspace.router) if 'POST' in getattr(r, 'methods', ())}
    aliases = {r.path: r for r in _routes(manager.router) if 'POST' in getattr(r, 'methods', ())}
    assert set(canonical) == POST_SUFFIXES == set(aliases)
    for path, route in canonical.items():
        assert route.endpoint is aliases[path].endpoint
        assert len([d for d in route.dependant.dependencies if d.call.__name__ == 'get_staff_user']) == 1


@pytest.mark.parametrize('role,impersonated', [('pi', False), ('reviewer', False), ('pi', True), ('reviewer', True)])
async def test_valid_write_bodies_cannot_bypass_effective_role(client, db_session, role, impersonated):
    effective = await factories.make_user(db_session, user_role=role)
    actor = await factories.make_user(db_session, user_role='admin') if impersonated else effective
    target = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=target)
    await db_session.flush()
    before = await _snapshot(db_session, target.id)
    headers = auth_headers(actor.id, impersonate=effective.id if impersonated else None)
    ids = {key: str(uuid.uuid4()) for key in ('candidate_id', 'publication_id', 'grant_id', 'funding_id', 'evidence_id', 'company_id')}
    ids['user_id'] = str(target.id)
    form = {'orcid': '0000-0001-0000-0011', 'name': 'Valid Name', 'email': '',
            'expected_profile_version': '1', 'profile_id': '123', 'company_name': 'Real Company',
            'pi_role': 'founder', 'research_summary': 'No mutation'}
    for prefix in ('/workspace', '/manager'):
        for suffix in sorted(POST_SUFFIXES):
            response = await client.post(prefix + suffix.format(**ids), data=form, headers=headers)
            assert response.status_code == 403, (prefix + suffix, response.text)
            assert await _snapshot(db_session, target.id) == before


async def test_assignment_is_sql_scope_not_authorization(client, db_session):
    reviewer = await factories.make_user(db_session, user_role='reviewer')
    other = await factories.make_user(db_session, user_role='reviewer')
    run = await factories.make_simulation_run(db_session)
    rows = [OpportunityAssessment(simulation_run_id=run.id, agent_id='hub', subject_agent_id=f'lab{i}',
            channel_name='hub-lab', recommendation='advance', band='advance', weighted_score=i,
            headline=f'Assessment {i}') for i in range(3)]
    db_session.add_all(rows)
    await db_session.flush()
    for row, person in [(rows[0], reviewer), (rows[0], other), (rows[1], other)]:
        db_session.add(AssessmentReviewAssignment(assessment_id=row.id, assignee_user_id=person.id,
            assignee_name=person.name, assigned_by_user_id=other.id, assigned_by_name=other.name))
    await db_session.flush()
    mine = await list_assessments(db_session, str(run.id), review='all', assignee_user_id=reviewer.id)
    assert [row.id for row in mine['assessments']] == [rows[0].id]
    assert mine['total_count'] == mine['review_counts']['all'] == 1
    assert mine['lab_options'] == ['lab0']
    all_rows = await list_assessments(db_session, str(run.id), review='all')
    assert all_rows['total_count'] == 3
    response = await client.get(f'/workspace/assessments/{rows[2].id}', headers=auth_headers(reviewer.id))
    assert response.status_code == 200
    assert 'Assessment 2' in response.text


async def test_legacy_resource_errors_and_multivalue_query(client, db_session):
    admin = await factories.make_user(db_session, user_role='admin')
    staff = await factories.make_user(db_session, user_role='manager')
    for prefix in ('/manager', '/admin'):
        response = await client.get(f'{prefix}/assessments/{uuid.uuid4()}', headers=auth_headers(admin.id))
        assert response.status_code == 404
    response = await client.get('/manager/discussions?agent_filter=a&agent_filter=b&export=false&page=2',
                                headers=auth_headers(staff.id), follow_redirects=False)
    assert response.status_code == 302
    assert response.headers['location'] == '/workspace/discussions?agent_filter=a&agent_filter=b&page=2'
    assert (await client.get('/workspace/discussions?export=false', headers=auth_headers(staff.id))).status_code == 403


@pytest.mark.parametrize('page', ['0', str(MAX_PAGE + 1), 'not-a-number'])
async def test_legacy_pi_directory_validates_page_before_redirect(client, db_session, page):
    reviewer = await factories.make_user(db_session, user_role='reviewer')
    for prefix in ('/manager', '/workspace'):
        response = await client.get(f'{prefix}/pis?page={page}', headers=auth_headers(reviewer.id),
                                    follow_redirects=False)
        assert response.status_code == 422
        assert 'location' not in response.headers


async def test_reviewer_pi_loader_html_and_filters_are_research_only(client, db_session):
    reviewer = await factories.make_user(db_session, user_role='reviewer')
    pi = await factories.make_user(db_session, email='PI_CONTACT_SECRET@example.invalid')
    await factories.make_profile(db_session, user=pi, research_summary='RESEARCH_SENTINEL',
                                  private_profile_seed='PRIVATE_SENTINEL')
    response = await client.get(f'/workspace/pis/{pi.id}?error=no_email&activation_blocked=1', headers=auth_headers(reviewer.id))
    assert response.status_code == 200
    assert 'RESEARCH_SENTINEL' in response.text
    for secret in ('PI_CONTACT_SECRET', 'PRIVATE_SENTINEL', 'Onboarding', 'Joined', 'Agent activated', 'Job History', 'Save Profile', '/verify-email'):
        assert secret not in response.text
    normal = await client.get('/workspace/pis', headers=auth_headers(reviewer.id))
    crafted = await client.get('/workspace/pis?status_filter=dead&claimed_filter=unclaimed', headers=auth_headers(reviewer.id))
    assert normal.text == crafted.text
    assert (await db_session.execute(select(User.id).where(User.id == pi.id))).scalar_one() == pi.id
