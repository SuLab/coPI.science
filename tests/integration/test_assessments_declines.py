"""The Assessments page lists hub declines that carried no assessment under
"Declined without an assessment", scoped by the page's run and lab filters."""
import pytest

from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

_SECTION = 'id="declined"'


async def _assessment(db_session, run, subject_agent_id):
    from src.models import OpportunityAssessment

    row = OpportunityAssessment(
        simulation_run_id=run.id, agent_id="blackbird",
        subject_agent_id=subject_agent_id, channel_name="general",
    )
    db_session.add(row)
    await db_session.flush()
    return row


async def _decline(db_session, run, subject_agent_id, reason_text, thread_id):
    from src.models import HubDecline

    row = HubDecline(
        simulation_run_id=run.id, agent_id="blackbird", subject_agent_id=subject_agent_id,
        channel_name="hub-interviews", thread_id=thread_id, slack_ts=None,
        reason_text=reason_text,
    )
    db_session.add(row)
    await db_session.flush()
    return row


async def test_decline_listed_with_pi_label_reason_and_closing_reply(client, db_session):
    admin = await factories.make_user(db_session, user_role="admin")
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, agent_id="lab-a")
    run = await factories.make_simulation_run(db_session)
    await _assessment(db_session, run, "lab-a")
    await _decline(
        db_session, run, "lab-a",
        "Declined for now because the target is unvalidated.\n\nSecond paragraph of the reply.",
        "1700000000.000100",
    )

    html = (await client.get(
        f"/workspace/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text

    assert _SECTION in html
    assert "Declined without an assessment" in html
    section = html.split(_SECTION, 1)[1]
    assert f"/workspace/pis/{pi.id}" in section
    assert "lab-a" in section
    assert "#hub-interviews" in section
    assert "Declined for now because the target is unvalidated." in section
    details = section.split("<details", 1)[1].split("</details>", 1)[0]
    assert "closing reply" in details
    assert "Second paragraph of the reply." in details
    # The one-line reason sits outside the disclosure; the rest of the reply only inside it.
    assert "Second paragraph of the reply." not in section.split("<details", 1)[0]


async def test_run_without_declines_has_no_section(client, db_session):
    admin = await factories.make_user(db_session, user_role="admin")
    run = await factories.make_simulation_run(db_session)
    await _assessment(db_session, run, "lab-a")
    other_run = await factories.make_simulation_run(db_session)
    await _decline(db_session, other_run, "lab-a", "Declined in another run.", "1700000000.000200")

    html = (await client.get(
        f"/workspace/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text

    assert _SECTION not in html
    assert "Declined without an assessment" not in html
    assert "Declined in another run." not in html


async def test_lab_filter_hides_another_labs_decline(client, db_session):
    admin = await factories.make_user(db_session, user_role="admin")
    run = await factories.make_simulation_run(db_session)
    await _assessment(db_session, run, "lab-a")
    await _assessment(db_session, run, "lab-b")
    await _decline(db_session, run, "lab-a", "Declined lab A pitch.", "1700000000.000300")
    await _decline(db_session, run, "lab-b", "Declined lab B pitch.", "1700000000.000400")

    html = (await client.get(
        f"/workspace/assessments?run_id={run.id}&lab=lab-a", headers=auth_headers(admin.id)
    )).text

    assert _SECTION in html
    assert "Declined lab A pitch." in html
    assert "Declined lab B pitch." not in html


async def test_reason_text_is_html_escaped(client, db_session):
    admin = await factories.make_user(db_session, user_role="admin")
    run = await factories.make_simulation_run(db_session)
    await _assessment(db_session, run, "lab-a")
    await _decline(
        db_session, run, "lab-a",
        "Declined.\n\n<script>alert(1)</script>",
        "1700000000.000500",
    )

    html = (await client.get(
        f"/workspace/assessments?run_id={run.id}", headers=auth_headers(admin.id)
    )).text

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)" not in html


async def test_reviewer_sees_the_section(client, db_session):
    reviewer = await factories.make_user(db_session, user_role="reviewer")
    run = await factories.make_simulation_run(db_session)
    await _assessment(db_session, run, "lab-a")
    await _decline(db_session, run, "lab-a", "Declined for the reviewer.", "1700000000.000600")

    response = await client.get(
        f"/workspace/assessments?run_id={run.id}", headers=auth_headers(reviewer.id)
    )

    assert response.status_code == 200
    assert _SECTION in response.text
    assert "Declined for the reviewer." in response.text


async def test_a_lab_with_only_declines_is_selectable(client, db_session):
    admin = await factories.make_user(db_session, user_role="admin")
    run = await factories.make_simulation_run(db_session)
    await _assessment(db_session, run, "lab-a")
    await _decline(db_session, run, "lab-b", "⏸️ Only a decline for this lab.", "1.1")
    html = (await client.get(
        f"/workspace/assessments?run_id={run.id}&lab=lab-b", headers=auth_headers(admin.id)
    )).text
    assert 'value="lab-b"' in html
    section = html.split(_SECTION, 1)[1]
    assert "Only a decline for this lab." in section


async def test_declines_are_not_on_the_reviewed_tab_or_an_assignee_view(client, db_session):
    admin = await factories.make_user(db_session, user_role="admin")
    run = await factories.make_simulation_run(db_session)
    await _assessment(db_session, run, "lab-a")
    await _decline(db_session, run, "lab-a", "⏸️ Declined.", "1.1")
    for query in ("review=reviewed", "assignment=mine"):
        html = (await client.get(
            f"/workspace/assessments?run_id={run.id}&{query}", headers=auth_headers(admin.id)
        )).text
        assert _SECTION not in html, query
    html = (await client.get(
        f"/workspace/assessments?run_id={run.id}&review=all", headers=auth_headers(admin.id)
    )).text
    assert _SECTION in html
