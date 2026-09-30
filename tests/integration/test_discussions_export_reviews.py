"""Historical proposal reviews leave the discussions page and join its export."""

from datetime import UTC, datetime

import pytest

from src.models import USER_ROLE_ADMIN, ProposalReview
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

REVIEWED = datetime(2026, 8, 1, 9, 30, tzinfo=UTC)


async def _world(db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi = await factories.make_user(db_session)
    run = await factories.make_simulation_run(db_session)
    await factories.make_agent_message(
        db_session, run=run, agent_id="su", channel_name="general",
        phase="new_post", message_ts="1700000000.000100", content="root",
    )
    td = await factories.make_thread_decision(
        db_session, run=run, thread_id="1700000000.000100", channel="general",
        agent_a="su", agent_b="lotz", outcome="proposal",
        summary_text="A joint assay platform.", decided_at=REVIEWED,
    )
    db_session.add(ProposalReview(
        thread_decision_id=td.id, agent_id="lotz", user_id=pi.id,
        rating=3, comment="REVIEW-COMMENT-XYZ", reviewed_at=REVIEWED,
    ))
    await db_session.flush()
    return admin, run


async def test_the_export_carries_historical_proposal_reviews(client, db_session):
    admin, run = await _world(db_session)
    txt = await client.get(
        f"/admin/discussions?run_id={run.id}&export=true", headers=auth_headers(admin.id)
    )
    assert txt.status_code == 200
    assert "REVIEW-COMMENT-XYZ" in txt.text
    assert "LotzBot's PI: 3/4" in txt.text
    assert "2026-08-01 09:30 UTC" in txt.text

    html = await client.get(
        f"/admin/discussions?run_id={run.id}&export=html", headers=auth_headers(admin.id)
    )
    assert html.status_code == 200
    assert "REVIEW-COMMENT-XYZ" in html.text
    assert "3/4" in html.text


async def test_the_discussions_page_renders_no_review_rows(client, db_session):
    admin, run = await _world(db_session)
    page = await client.get(f"/admin/discussions?run_id={run.id}", headers=auth_headers(admin.id))
    assert page.status_code == 200
    assert "REVIEW-COMMENT-XYZ" not in page.text
    assert "PI Reviews" not in page.text
    # The decision summary itself is still reachable from the row.
    assert "A joint assay platform." in page.text


async def test_the_agents_page_has_no_proposal_counts(client, db_session):
    admin, _run = await _world(db_session)
    await factories.make_agent(db_session, agent_id="lotz", status="active")
    await db_session.flush()
    page = await client.get("/admin/agents", headers=auth_headers(admin.id))
    assert page.status_code == 200
    assert "to review" not in page.text
    assert ">Proposals<" not in page.text
