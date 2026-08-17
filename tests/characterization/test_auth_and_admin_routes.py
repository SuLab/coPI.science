"""Characterization pins for auth, session-gated, and admin routes.

Unauthenticated protected routes are pinned as they behave today; a forged
signed session cookie (built exactly like starlette's SessionMiddleware) drives
the authenticated paths without a real ORCID OAuth round-trip.
"""

import base64
import json

import pytest
from itsdangerous import TimestampSigner

from src.config import get_settings
from src.models import USER_ROLE_ADMIN, USER_ROLE_PI
from tests import factories

pytestmark = pytest.mark.characterization

# AgentBadgeMiddleware's session-factory bypass is handled centrally by the `client`
# fixture (tests/conftest.py) — see the note there.


def _session_cookie(user_id) -> str:
    """Forge a 'copi-session' cookie the same way SessionMiddleware signs it."""
    signer = TimestampSigner(get_settings().secret_key)
    data = base64.b64encode(json.dumps({"user_id": str(user_id)}).encode())
    return signer.sign(data).decode("utf-8")


def _auth_headers(user_id) -> dict:
    return {"Cookie": f"copi-session={_session_cookie(user_id)}"}


# --- auth router -------------------------------------------------------------

async def test_login_page_anonymous_200(client):
    r = await client.get("/login")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


async def test_login_page_with_session_redirects_home(client):
    # login() short-circuits to "/" when the session already has a user_id.
    r = await client.get("/login", headers={"Cookie": f"copi-session={_session_cookie('irrelevant')}"})
    assert r.status_code == 302
    assert r.headers["location"] == "/"


async def test_login_start_redirects_to_orcid(client):
    r = await client.get("/login/start")
    assert r.status_code == 302
    assert "orcid.org" in r.headers["location"]


async def test_auth_callback_error_param_redirects(client):
    r = await client.get("/auth/callback", params={"error": "access_denied"})
    assert r.status_code == 302
    assert r.headers["location"] == "/login?error=oauth_error"


async def test_auth_callback_no_code_redirects(client):
    r = await client.get("/auth/callback")
    assert r.status_code == 302
    assert r.headers["location"] == "/login?error=no_code"


async def test_auth_callback_state_mismatch_fails_closed(client):
    # SEC-3: no stored state in a fresh session -> reject even with code+state.
    r = await client.get("/auth/callback", params={"code": "abc", "state": "forged"})
    assert r.status_code == 302
    assert r.headers["location"] == "/login?error=state_mismatch"


async def test_logout_post_redirects_to_login(client):
    r = await client.post("/logout")
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


async def test_logout_get_not_allowed(client):
    # SEC-8: logout is POST-only (was a CSRF target as a GET).
    r = await client.get("/logout")
    assert r.status_code == 405


# --- protected routes: unauthenticated redirect to /login -------------------

@pytest.mark.parametrize("path", ["/profile", "/settings", "/onboarding", "/agent"])
async def test_protected_route_unauth_redirects_login(client, path):
    r = await client.get(path)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")


async def test_admin_unauth_redirects_login(client):
    r = await client.get("/admin")
    assert r.status_code == 302
    assert r.headers["location"].startswith("/login")


# --- authenticated paths -----------------------------------------------------

async def test_profile_authenticated_200(client, db_session):
    u = await factories.make_user(db_session, onboarding_complete=True)
    r = await client.get("/profile", headers=_auth_headers(u.id))
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


async def test_profile_authed_incomplete_onboarding_redirects(client, db_session):
    u = await factories.make_user(db_session, onboarding_complete=False)
    r = await client.get("/profile", headers=_auth_headers(u.id))
    assert r.status_code == 302
    assert r.headers["location"] == "/onboarding"


async def test_settings_authenticated_200(client, db_session):
    u = await factories.make_user(db_session, onboarding_complete=True)
    r = await client.get("/settings", headers=_auth_headers(u.id))
    assert r.status_code == 200


async def test_onboarding_authed_complete_redirects_profile(client, db_session):
    u = await factories.make_user(db_session, onboarding_complete=True)
    r = await client.get("/onboarding", headers=_auth_headers(u.id))
    assert r.status_code == 302
    assert r.headers["location"] == "/profile"


async def test_onboarding_authed_incomplete_200(client, db_session):
    u = await factories.make_user(db_session, onboarding_complete=False)
    r = await client.get("/onboarding", headers=_auth_headers(u.id))
    assert r.status_code == 200


async def test_agent_landing_authenticated_200(client, db_session):
    u = await factories.make_user(db_session, onboarding_complete=True)
    r = await client.get("/agent", headers=_auth_headers(u.id))
    assert r.status_code == 200


async def test_admin_non_admin_user_403(client, db_session):
    u = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    r = await client.get("/admin", headers=_auth_headers(u.id))
    assert r.status_code == 403


async def test_admin_admin_user_200(client, db_session):
    u = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.get("/admin", headers=_auth_headers(u.id))
    assert r.status_code == 200


# --- token-gated public pages (no login) ------------------------------------

async def test_invite_invalid_token_renders_error_200(client):
    r = await client.get("/invite/not-a-real-token")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


async def test_unsubscribe_invalid_token_renders_error_200(client):
    r = await client.get("/settings/unsubscribe/bogus-token")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# --- /admin/discussions: nullable agent_id on Slack-imported posts ------------


async def test_admin_discussions_survives_a_bot_post_with_no_agent_id(client, db_session):
    """Regression: /admin/discussions 500'd in production.

    `_rebuild_state_from_slack` records real Slack messages whose sender cannot
    be mapped to a known bot as `is_bot=True, agent_id=NULL` — measured: 7 such
    rows, all from raw Slack user id U0BKJ6US485. The handler collected them
    into `available_agents` unguarded (every sibling `.add()` IS guarded) and
    then `sorted()` the set, so one NULL took the whole page down with
    `TypeError: '<' not supported between instances of 'NoneType' and 'str'`.
    """
    u = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await factories.make_simulation_run(db_session)
    # A normal bot post, so the set is genuinely mixed rather than all-None.
    await factories.make_agent_message(
        db_session, run=run, agent_id="gill", is_bot=True,
        message_ts="1786000000.000100", thread_ts=None, channel_name="general",
    )
    # The Slack-imported post with no mappable sender.
    await factories.make_agent_message(
        db_session, run=run, agent_id=None, is_bot=True,
        message_ts="1786000000.000200", thread_ts=None, channel_name="general",
        sender_name="U0BKJ6US485",
    )
    await db_session.flush()

    r = await client.get("/admin/discussions", headers=_auth_headers(u.id))
    assert r.status_code == 200
    # The NULL-agent thread's "Posted By" cell must not read as a real bot —
    # `{{ t.agent_id | capitalize }}Bot` printed the literal "NoneBot".
    assert "NoneBot" not in r.text
    assert "(unknown sender)" in r.text


async def test_admin_discussions_export_with_zero_runs_still_renders_html(client, db_session):
    """Regression: the services/directory extraction (Task 3) moved the
    "no simulation runs exist at all" early return to AFTER the `if export:`
    branch was evaluated, so `?export=true` on a fresh instance (zero
    `SimulationRun` rows) started returning a `PlainTextResponse` attachment
    ("No proposals found with current filters.") instead of the normal HTML
    discussions page — the only behaviour delta the refactor introduced.
    `build_discussions_view` signals this state via `selected_run_id=None`;
    the router must check that and return the HTML page before ever looking
    at `export`.
    """
    u = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)

    r = await client.get(
        "/admin/discussions", params={"export": "true"}, headers=_auth_headers(u.id)
    )
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Content-Disposition" not in r.headers


async def test_admin_activity_detail_survives_a_bot_post_with_no_agent_id(client, db_session):
    """Regression: /admin/activity/{run_id} 500'd the same way /admin/discussions
    did (fixed in 73a78c3 for that route only).

    `admin_activity_detail` builds `channel_stats[...]["agents"]` as a set and
    does `channel_stats[channel]["agents"].add(msg.agent_id)` with no guard, then
    `templates/admin/activity_detail.html` does `{{ stats.agents | sort | join(', ') }}`
    — Jinja's `sort` is `sorted()`, so one NULL `agent_id` (the same
    `_rebuild_state_from_slack` "sender maps to no known bot" case) takes the whole
    page down with `TypeError: '<' not supported between instances of 'NoneType'
    and 'str'`. This run is the one that is FIRST in the Activity table today, so
    it is the most likely click in production.
    """
    u = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await factories.make_simulation_run(db_session)
    # A normal bot post, so the set is genuinely mixed rather than all-None.
    await factories.make_agent_message(
        db_session, run=run, agent_id="gill", is_bot=True,
        message_ts="1786000000.000100", thread_ts=None, channel_name="general",
    )
    # The Slack-imported post with no mappable sender, in the same channel.
    await factories.make_agent_message(
        db_session, run=run, agent_id=None, is_bot=True,
        message_ts="1786000000.000200", thread_ts=None, channel_name="general",
        sender_name="U0BKJ6US485",
    )
    await db_session.flush()

    r = await client.get(f"/admin/activity/{run.id}", headers=_auth_headers(u.id))
    assert r.status_code == 200
    # The NULL-agent row must not be rendered as a lie: no literal "NoneBot"
    # anywhere on the page (Messages-by-Agent table, by-channel agent list, or
    # the message timeline).
    assert "NoneBot" not in r.text
    assert "(unknown sender)" in r.text


# --- /admin/activity/{run_id}/llm-calls: unbounded `page` ---------------------


async def test_admin_llm_calls_page_zero_rejected_not_500(client, db_session):
    """Regression: `page: int = 1` had no lower bound, so `?page=0` computed
    `offset = (0 - 1) * 50 = -50` and Postgres raised `OFFSET must not be
    negative` as an unhandled 500. `Query(1, ge=1)` rejects an out-of-range
    page with a clean 422 instead, and `?page=1` still works.
    """
    u = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    run = await factories.make_simulation_run(db_session)
    await db_session.flush()

    r = await client.get(
        f"/admin/activity/{run.id}/llm-calls",
        params={"page": 0},
        headers=_auth_headers(u.id),
    )
    assert r.status_code == 422

    r2 = await client.get(
        f"/admin/activity/{run.id}/llm-calls",
        params={"page": 1},
        headers=_auth_headers(u.id),
    )
    assert r2.status_code == 200
