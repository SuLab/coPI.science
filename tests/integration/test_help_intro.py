"""The first-sign-in prompt and its answer (spec docs/specs/2026-10-14-help-and-onboarding-design.md
§5.2, §5.3, §8.4, §10.2): who sees it, POST /help/intro/dismiss and its `next` check, that the
workspace router dependency changes no route's refusal, and the payload and login-resume
edits in src/dependencies.py.

Setup is committed before each request, because a refused route rolls the shared session
back and would otherwise take the setup with it.
"""
import uuid

import pytest
from sqlalchemy import select

from src.dependencies import pending_access_payload
from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI, USER_ROLE_REVIEWER
from src.models.help import HelpIntroDismissal
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

_PROMPT = 'aria-label="Getting started"'
_DISMISS = "/help/intro/dismiss"


async def _ids(db_session, *roles):
    ids = [(await factories.make_user(db_session, user_role=role)).id for role in roles]
    await db_session.commit()
    return ids


async def _answers(db_session, user_id) -> list[str]:
    rows = await db_session.scalars(
        select(HelpIntroDismissal.via).where(HelpIntroDismissal.user_id == user_id))
    return list(rows)


async def _post(client, headers, **data):
    return await client.post(_DISMISS, data=data, headers=headers, follow_redirects=False)


async def test_prompt_shown_until_answered(client, db_session):
    (reviewer,) = await _ids(db_session, USER_ROLE_REVIEWER)
    r = await client.get("/workspace/assessments", headers=auth_headers(reviewer))
    assert r.status_code == 200
    assert _PROMPT in r.text and 'href="/help/tutorials/reviewer"' in r.text
    r = await _post(client, auth_headers(reviewer), via="dismiss", next="/workspace/assessments")
    assert r.status_code == 302 and r.headers["location"] == "/workspace/assessments"
    assert await _answers(db_session, reviewer) == ["dismiss"]
    r = await client.get("/workspace/assessments", headers=auth_headers(reviewer))
    assert r.status_code == 200 and _PROMPT not in r.text


async def test_prompt_offers_manager_tutorial_to_manager(client, db_session):
    (manager,) = await _ids(db_session, USER_ROLE_MANAGER)
    r = await client.get("/workspace/pis", headers=auth_headers(manager))
    assert r.status_code == 200
    assert _PROMPT in r.text and 'href="/help/tutorials/manager"' in r.text


async def test_prompt_never_for_admin(client, db_session):
    (admin,) = await _ids(db_session, USER_ROLE_ADMIN)
    r = await client.get("/workspace/pis", headers=auth_headers(admin))
    assert r.status_code == 200 and _PROMPT not in r.text


async def test_prompt_hidden_while_impersonating(client, db_session):
    admin, reviewer, manager = await _ids(
        db_session, USER_ROLE_ADMIN, USER_ROLE_REVIEWER, USER_ROLE_MANAGER)
    r = await client.get("/workspace/assessments",
                         headers=auth_headers(admin, impersonate=reviewer))
    assert r.status_code == 200 and _PROMPT not in r.text
    r = await client.get("/workspace/pis", headers=auth_headers(admin, impersonate=manager))
    assert r.status_code == 200 and _PROMPT not in r.text


async def test_dismiss_refused_while_impersonating(client, db_session):
    admin, reviewer = await _ids(db_session, USER_ROLE_ADMIN, USER_ROLE_REVIEWER)
    r = await _post(client, auth_headers(admin, impersonate=reviewer), via="dismiss")
    assert r.status_code == 403
    assert "Help changes are disabled while impersonating." in r.text
    assert await _answers(db_session, reviewer) == []
    assert await _answers(db_session, admin) == []


async def test_dismiss_rejects_bad_via(client, db_session):
    (reviewer,) = await _ids(db_session, USER_ROLE_REVIEWER)
    r = await _post(client, auth_headers(reviewer), via="other")
    assert r.status_code == 400
    r = await _post(client, auth_headers(reviewer))
    assert r.status_code == 400
    assert await _answers(db_session, reviewer) == []


async def test_first_answer_kept(client, db_session):
    (reviewer,) = await _ids(db_session, USER_ROLE_REVIEWER)
    r = await _post(client, auth_headers(reviewer), via="finish")
    assert r.status_code == 302 and r.headers["location"] == "/help"
    r = await _post(client, auth_headers(reviewer), via="dismiss")
    assert r.status_code == 302
    assert await _answers(db_session, reviewer) == ["finish"]


@pytest.mark.parametrize("next_url,expected", [
    ("https://evil.example/", "/workspace"),
    ("//evil.example", "/workspace"),
    ("/\\evil", "/workspace"),
    ("/help/intro/dismiss", "/workspace"),
    ("/workspace/assessments?run_id=x", "/workspace/assessments?run_id=x"),
])
async def test_dismiss_next_is_validated(client, db_session, next_url, expected):
    (reviewer,) = await _ids(db_session, USER_ROLE_REVIEWER)
    r = await _post(client, auth_headers(reviewer), via="dismiss", next=next_url)
    assert r.status_code == 302 and r.headers["location"] == expected


async def test_workspace_post_routes_unaffected(client, db_session):
    reviewer, pi = await _ids(db_session, USER_ROLE_REVIEWER, USER_ROLE_PI)
    r = await client.post(f"/workspace/pis/{uuid.uuid4()}/mute", headers=auth_headers(reviewer),
                          follow_redirects=False)
    assert r.status_code == 403 and "Manager access required" in r.text
    r = await client.get("/workspace/slack-bots", headers=auth_headers(pi),
                         follow_redirects=False)
    assert r.status_code == 403
    assert "Manager access required" in r.text and "Review access required" not in r.text


async def test_pending_access_payload_has_role_and_status(db_session):
    user = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER,
                                     access_status="denied")
    payload = pending_access_payload(user)
    assert payload["user_role"] == USER_ROLE_REVIEWER
    assert payload["access_status"] == "denied"


async def test_login_location_skips_help_shots(client):
    r = await client.get("/help/shots/a.png", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"
