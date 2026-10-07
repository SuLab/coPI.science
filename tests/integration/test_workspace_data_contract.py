"""Assignment SQL scope, page privacy and loader-query cost at the actual boundary."""
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event

from src.models import AssessmentReview, AssessmentReviewAssignment, OpportunityAssessment
from src.services import directory
from src.services.web_permissions import capabilities_for
from src.web.presentation import project_workspace
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


@contextmanager
def _queries(db):
    statements = []
    connection = db.get_bind()

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement.lower())

    event.listen(connection, 'before_cursor_execute', capture)
    try:
        yield statements
    finally:
        event.remove(connection, 'before_cursor_execute', capture)


async def test_assignment_precedes_cap_counts_lab_and_review_filters(client, db_session, monkeypatch):
    reviewer = await factories.make_user(db_session, user_role='reviewer')
    other = await factories.make_user(db_session, user_role='reviewer')
    now = datetime.now(UTC)
    old = await factories.make_simulation_run(db_session, started_at=now - timedelta(days=1))
    current = await factories.make_simulation_run(db_session, started_at=now)
    rows = []
    for run, lab, score in [(current, 'hidden', 5), (current, 'lab-a', 4),
                            (current, 'lab-a', 3), (current, 'lab-b', 2), (old, 'older', 1)]:
        row = OpportunityAssessment(simulation_run_id=run.id, agent_id='hub',
                                    subject_agent_id=lab, channel_name='test', weighted_score=score)
        db_session.add(row)
        rows.append(row)
    await db_session.flush()
    for row in rows[1:]:
        db_session.add(AssessmentReviewAssignment(assessment_id=row.id, assignee_user_id=reviewer.id,
            assignee_name=reviewer.name, assigned_by_user_id=other.id, assigned_by_name=other.name))
    db_session.add(AssessmentReviewAssignment(assessment_id=rows[1].id, assignee_user_id=other.id,
        assignee_name=other.name, assigned_by_user_id=other.id, assigned_by_name=other.name))
    db_session.add(AssessmentReview(assessment_id=rows[1].id, reviewer_user_id=other.id,
        reviewer_name=other.name, score=4, comment='A real review', feedback_mode='learn'))
    await db_session.flush()
    monkeypatch.setattr(directory, 'ASSESSMENTS_LIMIT', 1)
    mine = await directory.list_assessments(db_session, None, review='all', assignee_user_id=reviewer.id)
    assert mine['selected_run_id'] == current.id
    assert [row.id for row in mine['assessments']] == [rows[1].id]  # Not hidden high-scoring row.
    assert mine['total_count'] == 3
    assert mine['review_counts'] == {'all': 3, 'reviewed': 1, 'unreviewed': 2}
    assert mine['lab_options'] == ['lab-a', 'lab-b']
    assert mine['incomplete_panel_count'] == 4  # Run-wide, intentionally not assignment-scoped.
    narrowed = await directory.list_assessments(db_session, str(current.id), lab='lab-a',
        review='unreviewed', assignee_user_id=reviewer.id)
    assert [row.id for row in narrowed['assessments']] == [rows[2].id]
    assert narrowed['total_count'] == 1
    assert narrowed['review_counts'] == {'all': 2, 'reviewed': 1, 'unreviewed': 1}
    assert narrowed['lab_options'] == ['lab-a', 'lab-b']
    zero = await directory.list_assessments(db_session, None, review='all', assignee_user_id=other.id)
    assert zero['total_count'] == 1
    absent = await factories.make_user(db_session, user_role='reviewer')
    empty = await directory.list_assessments(db_session, None, assignee_user_id=absent.id)
    assert empty['selected_run_id'] == current.id and not empty['show_all_runs']
    assert empty['total_count'] == 0 and empty['lab_options'] == []
    all_runs = await directory.list_assessments(db_session, 'all', review='all', assignee_user_id=reviewer.id)
    assert all_runs['total_count'] == 4 and 'older' in all_runs['lab_options']
    html = (await client.get(f'/workspace/assessments?run_id={current.id}&assignment=mine&lab=lab-a&review=all',
                            headers=auth_headers(reviewer.id))).text
    assert '4 stored across all labs and assignments' in html
    assert 'All (2)' in html and 'Reviewed (1)' in html and 'Unreviewed (1)' in html
    assert 'across all labs and assignments.' in html
    assert 'Tab counts follow the selected run, lab and assignment filters.' in html
    empty_html = (await client.get('/workspace/assessments?assignment=mine', headers=auth_headers(absent.id))).text
    assert 'No assessments match the current run, lab and assignment filters' in empty_html
    assert '4 stored across all labs and assignments' in empty_html
    assert 'the run menu shows stored counts across all labs and assignments.' in empty_html


async def test_reviewer_run_config_is_projected_before_template_and_absent_from_html(client, db_session, monkeypatch):
    from src.routers.workspace import assessments

    reviewer = await factories.make_user(db_session, user_role='reviewer')
    await factories.make_simulation_run(db_session, config={
        'rubric_version': 'RESEARCH_VERSION', 'prompt_stamps': {'arbitrary': 'PRIVATE_PROMPT_STAMP'},
        'announcement_text': 'PRIVATE_RUN_ANNOUNCEMENT',
    })
    captured = []
    render = assessments.templates.TemplateResponse

    def capture(request, template, context, **kwargs):
        captured.append(context)
        return render(request, template, context, **kwargs)

    monkeypatch.setattr(assessments.templates, 'TemplateResponse', capture)
    response = await client.get('/workspace/assessments', headers=auth_headers(reviewer.id))
    assert response.status_code == 200 and 'RESEARCH_VERSION' in response.text
    assert captured[0]['runs'][0].config == {'rubric_version': 'RESEARCH_VERSION'}
    for secret in ('PRIVATE_PROMPT_STAMP', 'PRIVATE_RUN_ANNOUNCEMENT'):
        assert secret not in repr(captured) and secret not in response.text


async def test_assignment_uses_effective_id_not_client_or_actor(client, db_session):
    actor = await factories.make_user(db_session, user_role='admin')
    effective = await factories.make_user(db_session, user_role='reviewer')
    run = await factories.make_simulation_run(db_session)
    rows = []
    for person, label in [(actor, 'ACTOR ONLY'), (effective, 'EFFECTIVE ONLY')]:
        row = OpportunityAssessment(simulation_run_id=run.id, agent_id='hub', channel_name='test',
                                    headline=label)
        db_session.add(row)
        await db_session.flush()
        db_session.add(AssessmentReviewAssignment(assessment_id=row.id, assignee_user_id=person.id,
            assignee_name=person.name, assigned_by_user_id=actor.id, assigned_by_name=actor.name))
        rows.append(row)
    await db_session.flush()
    response = await client.get(f'/workspace/assessments?assignment=mine&assignee_user_id={actor.id}',
                                headers=auth_headers(actor.id, impersonate=effective.id))
    assert response.status_code == 200
    assert 'EFFECTIVE ONLY' in response.text and 'ACTOR ONLY' not in response.text
    all_rows = await client.get('/workspace/assessments?assignment=https%3A%2F%2Fevil.invalid%0D%0A',
                               headers=auth_headers(effective.id))
    assert all_rows.status_code == 200 and all(label in all_rows.text for label in ('ACTOR ONLY', 'EFFECTIVE ONLY'))


async def test_research_loader_selects_no_account_private_or_job_columns(db_session):
    pi = await factories.make_user(db_session, email='ACCOUNT CONTACT SENTINEL')
    await factories.make_profile(db_session, user=pi, private_profile_seed='PRIVATE SENTINEL',
                                research_summary='CURRENT RESEARCH SENTINEL')
    db_session.expunge_all()
    with _queries(db_session) as statements:
        detail = await directory.load_pi_detail(db_session, pi.id, staff=False)
        reviewer = type('Viewer', (), {'user_role': 'reviewer'})()
        page = project_workspace('pis', {'target_user': detail['user'], 'profile': detail['profile']},
                                 capabilities_for(reviewer))
    assert statements
    sql = '\n'.join(statements)
    assert 'from jobs' not in sql
    for column in ('users.email', 'users.onboarding_complete', 'users.claimed_at',
                   'researcher_profiles.private_profile_seed', 'researcher_profiles.pending_profile',
                   'agent_registry.slack_bot_token'):
        assert column not in sql
    assert 'CURRENT RESEARCH SENTINEL' in repr(page)
    assert 'ACCOUNT CONTACT SENTINEL' not in repr(page) and 'PRIVATE SENTINEL' not in repr(page)


async def test_account_loader_skips_pi_grants_and_industry(db_session):
    user = await factories.make_user(db_session, user_role='manager')
    with _queries(db_session) as statements:
        detail = await directory.load_account_detail(db_session, user.id)
    assert detail['user'].id == user.id
    sql = '\n'.join(statements)
    for table in ('pi_grants', 'pi_grant_identity', 'pi_orcid_fundings', 'pi_industry_evidence', 'pi_industry_scores'):
        assert f'from {table}' not in sql


@pytest.mark.parametrize('research_only', [True, False])
async def test_directory_queries_are_bounded_as_rows_grow(db_session, research_only):
    async def add_pis(number):
        for _ in range(number):
            pi = await factories.make_user(db_session)
            await factories.make_profile(db_session, user=pi)
            await factories.make_agent(db_session, user=pi)

    await add_pis(3)
    db_session.expunge_all()
    with _queries(db_session) as small:
        rows = await directory.list_pi_directory(db_session, roles=('pi',), research_only=research_only)
    assert len(rows) == 3
    await add_pis(17)
    db_session.expunge_all()
    with _queries(db_session) as large:
        rows = await directory.list_pi_directory(db_session, roles=('pi',), research_only=research_only)
    assert len(rows) == 20
    assert len(large) == len(small) and len(large) <= 12
    if research_only:
        assert 'from jobs' not in '\n'.join(large)
