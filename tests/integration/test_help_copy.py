"""Sign-in, access-pending and delete-account copy for managers and reviewers (spec
docs/specs/2026-10-14-help-and-onboarding-design.md §7.4, §10.2)."""
import re

import pytest

from src.models import USER_ROLE_MANAGER, USER_ROLE_PI, USER_ROLE_REVIEWER
from tests import factories
from tests.integration.test_manager_access import auth_headers
from tests.oidc_fakes import make_config
from tests.session_support import raw_session_headers

pytestmark = pytest.mark.integration

_TURNED_OFF = "Your access to Blackbird has been turned off."
_PENDING = "Blackbird is in pre-release."
#: The Cancel link's own target; base.html links to /settings in the nav as well.
_CANCEL = re.compile(r'<a href="([^"]*)"[^>]*> Cancel </a>')


def _flat(html: str) -> str:
    """The page with every whitespace run collapsed, so copy that wraps in the template
    matches as one sentence."""
    return " ".join(html.split())


async def _access_pending(client, payload: dict) -> str:
    r = await client.get(
        "/access-pending", headers=raw_session_headers({"pending_access": payload})
    )
    assert r.status_code == 200
    return _flat(r.text)


def _payload(role: str | None, status: str | None) -> dict:
    payload = {
        "user_id": "00000000-0000-0000-0000-000000000001",
        "orcid": "0000-0002-1234-5678",
        "email": None,
        "name": "Example Person",
    }
    if role is not None:
        payload["user_role"] = role
    if status is not None:
        payload["access_status"] = status
    return payload


async def test_a_denied_reviewer_is_told_to_ask_a_manager_or_an_admin(client):
    page = await _access_pending(client, _payload(USER_ROLE_REVIEWER, "denied"))
    assert "<h1" in page and "Access turned off</h1>" in page
    assert f"{_TURNED_OFF} Ask a manager or an admin to restore it." in page
    assert "Thanks for signing in" not in page
    assert 'action="/access-pending/email"' not in page
    assert "← Back to home" in page


async def test_a_denied_manager_is_told_to_ask_an_admin(client):
    page = await _access_pending(client, _payload(USER_ROLE_MANAGER, "denied"))
    assert "Access turned off</h1>" in page
    assert f"{_TURNED_OFF} Ask an admin to restore it." in page
    assert "Ask a manager" not in page
    assert 'action="/access-pending/email"' not in page


@pytest.mark.parametrize(
    ("role", "status"),
    [
        (USER_ROLE_PI, "pending"),
        (USER_ROLE_PI, "denied"),
        (None, None),  # a session saved before the payload carried the role and status
    ],
)
async def test_every_other_case_keeps_the_pending_copy(client, role, status):
    page = await _access_pending(client, _payload(role, status))
    assert "Thanks for signing in</h1>" in page
    assert _PENDING in page
    assert 'action="/access-pending/email"' in page
    assert "Access turned off" not in page
    assert "CoPI" not in page


async def test_a_reviewer_sees_the_staff_consequences_and_cancels_to_settings(
    client, db_session
):
    user = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    r = await client.get("/profile/delete-account", headers=auth_headers(user.id))
    assert r.status_code == 200
    page = _flat(r.text)
    assert "approve/disapprove history are kept, with your name" in page
    assert "Your review assignments are deleted." in page
    assert "Your assessment-chat history is deleted." in page
    assert "lab agent" not in page
    assert "allowlist" not in page
    assert "Assignments you gave to others" not in page
    assert _CANCEL.findall(page) == ["/settings"]


async def test_a_manager_also_sees_what_happens_to_their_attributions(client, db_session):
    user = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    r = await client.get("/profile/delete-account", headers=auth_headers(user.id))
    assert r.status_code == 200
    page = _flat(r.text)
    assert "Assignments you gave to others" in page
    assert "no longer linked to your account" in page
    assert "lab agent" not in page
    assert "allowlist" not in page


async def test_a_pi_sees_today_s_list_and_cancels_to_profile_edit(client, db_session):
    user = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    r = await client.get("/profile/delete-account", headers=auth_headers(user.id))
    assert r.status_code == 200
    page = _flat(r.text)
    assert "Your lab agent will be shut down and its Slack access revoked." in page
    assert "All publications imported from ORCID" in page
    assert "Your review assignments are deleted." not in page
    assert _CANCEL.findall(page) == ["/profile/edit"]


async def test_the_login_page_shows_both_info_boxes(client):
    page = _flat((await client.get("/login")).text)
    assert "For PIs, your ORCID record also seeds your research profile." in page
    assert "How it works for PIs" in page
    assert "Managers and reviewers:" in page
    assert "sign in with the ORCID iD your account was created with." in page
    assert "the Help tab has a tutorial for your role." in page
    assert "once you have linked it in Settings" not in page


async def test_the_login_page_names_microsoft_only_when_it_is_on(client, asgi_app):
    asgi_app.state.microsoft_sign_in = make_config()
    page = _flat((await client.get("/login")).text)
    assert (
        "the ORCID iD your account was created with, or with Microsoft once you have linked"
        " it in Settings." in page
    )
