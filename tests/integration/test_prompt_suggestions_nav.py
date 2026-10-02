"""Spec 2026-10-02 §4 (F1, O8): an admin who is not impersonating keeps the ADMIN sub-nav
on the two prompt-suggestion pages, on the list, the detail, and the pages the Generate
and status POSTs redirect to. A manager, and an admin impersonating a manager, keep the
manager sub-nav; an admin impersonating a reviewer or a PI is refused as before."""

import re

import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI, USER_ROLE_REVIEWER
from tests import factories
from tests.integration._webui_helpers import impersonation_headers
from tests.integration.test_manager_access import auth_headers
from tests.integration.test_prompt_suggestions_page import _seed_suggestion

pytestmark = pytest.mark.integration

_ADMIN_NAV = 'aria-label="Admin sections"'
_MANAGER_NAV = 'aria-label="Manager sections"'
_HIGHLIGHTED = (
    '<a href="/manager/prompt-suggestions" class="text-indigo-600 font-semibold">'
    "Prompt Suggestions</a>"
)
_TOPBAR_ADMIN_ACTIVE = re.compile(r'href="/admin/users" class="[^"]*text-indigo-600 font-semibold"')
_ADMIN_SECTIONS = (
    "/admin/users", "/admin/jobs", "/admin/agents", "/admin/cohorts", "/admin/access-requests",
    "/admin/simulation",
)


def _nav(body: str, label: str) -> str:
    """The text of the <nav> carrying ``label``, or "" when the page has none."""
    start = body.find(label)
    return "" if start < 0 else body[start: body.index("</nav>", start)]


def _assert_admin_nav(body: str) -> None:
    admin_nav = _nav(body, _ADMIN_NAV)
    assert admin_nav, "the admin sub-nav is missing"
    for href in _ADMIN_SECTIONS:
        assert f'href="{href}"' in admin_nav, href
    assert _HIGHLIGHTED in admin_nav
    assert _MANAGER_NAV not in body
    assert _TOPBAR_ADMIN_ACTIVE.search(body), "the top-bar Admin link lost its highlight"
    assert 'href="/manager"' not in body  # a plain admin still has no top-bar Manager link


def _assert_manager_nav(body: str) -> None:
    manager_nav = _nav(body, _MANAGER_NAV)
    assert manager_nav, "the manager sub-nav is missing"
    assert _HIGHLIGHTED in manager_nav
    assert _ADMIN_NAV not in body
    assert not _TOPBAR_ADMIN_ACTIVE.search(body)


async def test_a_plain_admin_keeps_the_admin_nav_on_the_list_and_the_detail(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    s = await _seed_suggestion(db_session)
    for path in ("/manager/prompt-suggestions", "/manager/prompt-suggestions?status=open",
                 f"/manager/prompt-suggestions/{s.id}"):
        r = await client.get(path, headers=auth_headers(admin.id))
        assert r.status_code == 200, path
        _assert_admin_nav(r.text)


async def test_a_plain_admin_keeps_the_admin_nav_after_generate_and_status(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    s = await _seed_suggestion(db_session)

    r = await client.post("/reviews/suggestions/generate", headers=auth_headers(admin.id),
                          follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/manager/prompt-suggestions?generated=")
    landed = await client.get(r.headers["location"], headers=auth_headers(admin.id))
    _assert_admin_nav(landed.text)

    r = await client.post(f"/reviews/suggestions/{s.id}/status", data={"action": "dismissed"},
                          headers=auth_headers(admin.id), follow_redirects=False)
    assert r.headers["location"] == f"/manager/prompt-suggestions/{s.id}"
    landed = await client.get(r.headers["location"], headers=auth_headers(admin.id))
    _assert_admin_nav(landed.text)


async def test_a_manager_keeps_the_manager_nav_everywhere(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    s = await _seed_suggestion(db_session)
    for path in ("/manager/prompt-suggestions", f"/manager/prompt-suggestions/{s.id}"):
        _assert_manager_nav((await client.get(path, headers=auth_headers(manager.id))).text)

    r = await client.post("/reviews/suggestions/generate", headers=auth_headers(manager.id),
                          follow_redirects=False)
    _assert_manager_nav((await client.get(r.headers["location"], headers=auth_headers(manager.id))).text)
    r = await client.post(f"/reviews/suggestions/{s.id}/status", data={"action": "dismissed"},
                          headers=auth_headers(manager.id), follow_redirects=False)
    _assert_manager_nav((await client.get(r.headers["location"], headers=auth_headers(manager.id))).text)


async def test_an_admin_wearing_a_manager_keeps_the_manager_nav(client, db_session):
    """Both POSTs refuse impersonation (they spend money or set attribution), so the
    pages they redirect to are read directly, with the query string Generate adds."""
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    s = await _seed_suggestion(db_session)
    headers = impersonation_headers(admin.id, manager.id)
    for path in ("/manager/prompt-suggestions",
                 "/manager/prompt-suggestions?generated=0&eligible=0",
                 f"/manager/prompt-suggestions/{s.id}"):
        r = await client.get(path, headers=headers)
        assert r.status_code == 200, path
        _assert_manager_nav(r.text)
        assert 'action="/admin/impersonate/stop"' in r.text
    r = await client.post("/reviews/suggestions/generate", headers=headers, follow_redirects=False)
    assert r.status_code == 403


@pytest.mark.parametrize("role", [USER_ROLE_REVIEWER, USER_ROLE_PI])
async def test_an_admin_wearing_a_reviewer_or_a_pi_is_still_refused(client, db_session, role):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    worn = await factories.make_user(db_session, user_role=role)
    s = await _seed_suggestion(db_session)
    headers = impersonation_headers(admin.id, worn.id)
    for path in ("/manager/prompt-suggestions", f"/manager/prompt-suggestions/{s.id}"):
        r = await client.get(path, headers=headers, follow_redirects=False)
        assert r.status_code == 403, path
        assert _ADMIN_NAV not in r.text
