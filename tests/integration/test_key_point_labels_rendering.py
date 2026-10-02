"""Label-aware key-point rendering on the detail brief and the list card (scout_hub
1.10.0, spec §6.1): a group whose extras all carry a known label renders its main
bullet as text and each extra on its own line with the label in bold; any other
multi-bullet group keeps its list."""
import pytest

from src.models import USER_ROLE_ADMIN, OpportunityAssessment
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

KEY_POINTS = {
    "lab_background": ["LBMAIN lab background main.", "**Companies:** COMPANYEXTRA Delfi (2014)."],
    "proposal": ["PROPONE first.", "PROPTWO second."],
    "commercial_opportunity": ["COMAIN commercial main.", "Risk: RISKEXTRA ownership unresolved."],
}


@pytest.fixture
async def admin(db_session):
    return await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)


async def _seed(db_session):
    run = await factories.make_simulation_run(db_session)
    row = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id="wang",
        channel_name="key-point-labels", company_or_project="Key Point Labels Co",
        recommendation="conditional", weighted_score=3.2, band="conditional",
        key_points=KEY_POINTS,
    )
    db_session.add(row)
    await db_session.flush()
    return run, row


async def test_the_detail_brief_renders_labelled_extras_as_lines(client, db_session, admin):
    _run, row = await _seed(db_session)
    html = (await client.get(
        f"/admin/assessments/{row.id}", headers=auth_headers(admin.id)
    )).text
    brief = html[html.index('id="brief"'):html.index('id="signals"')]
    assert "<p>COMAIN commercial main.</p>" in brief
    assert (
        '<p class="key-point-extra key-point-risk"><span class="font-semibold">Risk:</span>'
        " RISKEXTRA ownership unresolved.</p>"
    ) in brief
    assert (
        '<p class="key-point-extra key-point-companies"><span class="font-semibold">'
        "Companies:</span> COMPANYEXTRA Delfi (2014).</p>"
    ) in brief
    assert "**Companies:**" not in brief
    assert "<li>RISKEXTRA" not in brief and "<li>COMPANYEXTRA" not in brief
    # A group with an unlabelled second bullet keeps today's list.
    assert "<li>PROPONE first.</li>" in brief and "<li>PROPTWO second.</li>" in brief


async def test_the_list_card_renders_labelled_extras_as_lines(client, db_session, admin):
    run, _row = await _seed(db_session)
    html = (await client.get(
        f"/admin/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text
    points = html[html.index("assessment-card-points"):html.index("assessment-card-score-rationale")]
    assert '<p class="text-sm text-gray-700">COMAIN commercial main.</p>' in points
    assert (
        '<p class="key-point-extra key-point-risk text-sm text-gray-700">'
        '<span class="font-semibold">Risk:</span> RISKEXTRA ownership unresolved.</p>'
    ) in points
    assert (
        '<p class="key-point-extra key-point-companies text-sm text-gray-700">'
        '<span class="font-semibold">Companies:</span> COMPANYEXTRA Delfi (2014).</p>'
    ) in points
    assert "<li>PROPONE first.</li>" in points and "<li>PROPTWO second.</li>" in points
