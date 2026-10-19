"""The add-feedback forms preselect Learn as the feedback mode."""
import pytest

from src.models import AssessmentReviewAssignment, OpportunityAssessment
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

LEARN = '<option value="learn" selected>Learn</option>'


async def _assigned_row(db_session):
    reviewer = await factories.make_user(db_session, user_role='reviewer')
    other = await factories.make_user(db_session, user_role='reviewer')
    run = await factories.make_simulation_run(db_session)
    row = OpportunityAssessment(simulation_run_id=run.id, agent_id='hub',
                                subject_agent_id='lab-a', channel_name='test', weighted_score=4)
    db_session.add(row)
    await db_session.flush()
    db_session.add(AssessmentReviewAssignment(assessment_id=row.id, assignee_user_id=reviewer.id,
        assignee_name=reviewer.name, assigned_by_user_id=other.id, assigned_by_name=other.name))
    await db_session.flush()
    return reviewer, row


async def test_quick_scoring_preselects_learn(client, db_session):
    reviewer, _row = await _assigned_row(db_session)
    html = (await client.get('/workspace/assessments?review=all&assignment=mine',
                             headers=auth_headers(reviewer.id))).text
    start = html.index('name="feedback_mode"')
    form = html[start:html.index('</select>', start)]
    assert LEARN in form
    assert 'disabled selected' not in form
    assert 'name="feedback_mode" required' in html


async def test_detail_add_form_preselects_learn(client, db_session):
    reviewer, row = await _assigned_row(db_session)
    html = (await client.get(f'/workspace/assessments/{row.id}',
                             headers=auth_headers(reviewer.id))).text
    add = html[html.index('id="add-mode"'):]
    add = add[:add.index('</select>')]
    assert LEARN in add
    assert 'disabled selected' not in add
    assert 'name="feedback_mode" required' in add
