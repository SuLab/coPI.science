"""The §7 wording fixes as rendered (spec docs/specs/2026-10-14-help-and-onboarding-design.md
§7.1, §7.3, §10.2): state labels on the PI list and Slack Bots, the staff-only PI filter
form, the assessment detail's approval status, assign label and Learn help, and the
product name in every Workspace page title.

Setup is committed before the requests, as in test_staff_routes.py.
"""
import re

import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_REVIEWER
from tests import factories
from tests.integration.test_manager_access import auth_headers
from tests.integration.test_prompt_suggestions_page import _seed_suggestion
from tests.integration.test_reviews_router import _seed_assessment

pytestmark = pytest.mark.integration

_TITLE = re.compile(r"<title>(.*?)</title>", re.S)
_FILTER_FORM = '<form method="get"'
_STAFF_LEARN = (
    "Suggestions are generated only when a human presses the button on the "
    "prompt-suggestions page."
)
_REVIEWER_LEARN = "A manager decides when suggestions are generated."


def _flat(body: str) -> str:
    """Whitespace collapsed, so a sentence wrapped across template lines still matches."""
    return " ".join(body.split())


async def _seed_pis(db):
    """One PI per agent state the list labels, plus one with a staged draft."""
    muted = await factories.make_user(db, name="Muted Lab")
    await factories.make_agent(db, user=muted, status="inactive")
    pending_token = await factories.make_user(db, name="Pending Token Lab")
    await factories.make_agent(db, user=pending_token, status="pending",
                               slack_bot_token="xoxb-test-token")
    pending_bare = await factories.make_user(db, name="Pending Bare Lab")
    await factories.make_agent(db, user=pending_bare, status="pending")
    active = await factories.make_user(db, name="Active Lab")
    await factories.make_agent(db, user=active, status="active")
    await factories.make_profile(db, user=active)
    drafted = await factories.make_user(db, name="Drafted Lab")
    await factories.make_profile(
        db, user=drafted, pending_profile={"fields": {"research_summary": "A draft."}}
    )
    return active.id


async def test_pi_list_labels_as_a_manager(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    mgr_id = mgr.id
    await _seed_pis(db_session)
    await db_session.commit()

    body = (await client.get("/workspace/pis", headers=auth_headers(mgr_id))).text
    for label in ("Muted", "Pending", "Active", "Pending draft"):
        assert f">{label}</span>" in body, label
    assert re.search(r'<option value="pending_update"[^>]*>Pending draft</option>', body)
    assert _FILTER_FORM in body
    for stale in (">Inactive<", "Awaiting Token", "Pending Update"):
        assert stale not in body, stale


async def test_pi_list_has_no_filter_form_for_a_reviewer(client, db_session):
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    rev_id, admin_id = rev.id, admin.id
    await _seed_pis(db_session)
    await db_session.commit()

    for headers in (auth_headers(rev_id), auth_headers(admin_id, impersonate=rev_id)):
        resp = await client.get("/workspace/pis", headers=headers)
        assert resp.status_code == 200
        assert _FILTER_FORM not in resp.text


async def test_slack_bots_labels_as_a_manager(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    mgr_id = mgr.id
    await _seed_pis(db_session)
    await db_session.commit()

    body = (await client.get("/workspace/slack-bots", headers=auth_headers(mgr_id))).text
    assert ">Muted</span>" in body
    assert ">Pending</span>" in body
    assert ">Active</span>" in body


async def test_assessment_detail_as_a_reviewer(client, db_session):
    rev = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    rev_id = rev.id
    assessment = await _seed_assessment(db_session)
    assessment_id = assessment.id
    await db_session.commit()

    resp = await client.get(
        f"/workspace/assessments/{assessment_id}", headers=auth_headers(rev_id)
    )
    assert resp.status_code == 200
    body = _flat(resp.text)
    assert "Approval status" in body
    assert "No approval decision" in body
    assert "Assign to" not in body
    assert _REVIEWER_LEARN in body
    assert _STAFF_LEARN not in body


async def test_assessment_detail_as_a_manager(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    mgr_id = mgr.id
    assessment = await _seed_assessment(db_session)
    assessment_id = assessment.id
    await db_session.commit()

    resp = await client.get(
        f"/workspace/assessments/{assessment_id}", headers=auth_headers(mgr_id)
    )
    assert resp.status_code == 200
    body = _flat(resp.text)
    assert "Assign to" in body
    assert "Assign reviewer" not in body
    assert _STAFF_LEARN in body
    assert _REVIEWER_LEARN not in body


async def test_every_workspace_title_names_blackbird(client, db_session):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    mgr_id = mgr.id
    pi_id = await _seed_pis(db_session)
    assessment = await _seed_assessment(db_session)
    run = await factories.make_simulation_run(db_session)
    suggestion = await _seed_suggestion(db_session)
    assessment_id, run_id, suggestion_id = assessment.id, run.id, suggestion.id
    await db_session.commit()

    paths = (
        "/workspace/pis",
        f"/workspace/pis/{pi_id}",
        "/workspace/assessments",
        f"/workspace/assessments/{assessment_id}",
        "/workspace/slack-bots",
        "/workspace/staff",
        "/workspace/discussions",
        "/workspace/activity",
        f"/workspace/activity/{run_id}",
        "/workspace/prompt-suggestions",
        f"/workspace/prompt-suggestions/{suggestion_id}",
    )
    for path in paths:
        resp = await client.get(path, headers=auth_headers(mgr_id))
        assert resp.status_code == 200, path
        title = _TITLE.search(resp.text)
        assert title, path
        assert title.group(1).strip().endswith("— Blackbird"), (path, title.group(1))
