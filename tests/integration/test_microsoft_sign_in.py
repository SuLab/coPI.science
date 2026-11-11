"""Microsoft sign-in and linking flows (spec 2026-10-21 §5.3-§5.6)."""

import logging
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import func, select

from src.models import User, UserMicrosoftIdentity
from tests import factories
from tests.oidc_fakes import (
    OID,
    OTHER_TENANT,
    TENANT,
    make_config,
    microsoft_claims,
    sign,
)
from tests.session_support import raw_session_headers, session_from_response, session_headers

pytestmark = pytest.mark.integration

NGINX_HEADER_BUDGET = 3584  # under nginx's 4 KB proxy_buffer_size, with room for the rest


@pytest.fixture
def on(asgi_app):
    asgi_app.state.microsoft_sign_in = make_config()


def _flow(purpose="signin", *, user_id=None, started_at=None):
    return {
        "state": "st",
        "nonce": "nn",
        "code_verifier": "v" * 64,
        "purpose": purpose,
        "user_id": user_id,
        "started_at": int(time.time()) if started_at is None else started_at,
    }


def _token(fake_idp, **claims):
    fake_idp.token_response = (
        200,
        {
            "token_type": "Bearer",
            "access_token": "opaque",
            "id_token": sign(microsoft_claims(nonce="nn", **claims)),
        },
    )


async def _linked(db_session, user, oid=OID):
    db_session.add(
        UserMicrosoftIdentity(
            user_id=user.id, tenant_id=uuid.UUID(TENANT), object_id=uuid.UUID(oid)
        )
    )
    await db_session.flush()


async def _link_row(db_session, user):
    return await db_session.scalar(
        select(UserMicrosoftIdentity).where(UserMicrosoftIdentity.user_id == user.id)
    )


async def _sign_in_callback(client, query="code=c&state=st", flow=None):
    session = {} if flow is False else {"microsoft_flow": flow or _flow()}
    return await client.get(
        f"/auth/microsoft/callback?{query}", headers=raw_session_headers(session)
    )


def _fresh(user, **extra):
    """A session that began with a proven ORCID re-sign-in 10 s ago."""
    now = int(time.time())
    stamps = {"signed_in_at": now - 10, "signed_in_via": "orcid", "orcid_reauth_at": now - 10}
    return session_headers(user.id, **{**stamps, **extra})


def _app_messages(caplog) -> list[str]:
    """Messages the app's own loggers emitted (httpx logs request URLs too)."""
    return [r.getMessage() for r in caplog.records if r.name.startswith("src.")]


def _header_bytes(response):
    """Status line plus every header, the way nginx's upstream-header buffer sees them."""
    return len("HTTP/1.1 302 Found\r\n") + sum(
        len(k) + len(v) + 4 for k, v in response.headers.multi_items()
    )


# --- feature switch --------------------------------------------------------------


async def test_every_microsoft_route_is_404_while_off(client, db_session):
    user = await factories.make_user(db_session)
    for path in ("/auth/microsoft/start", "/auth/microsoft/callback?code=c&state=s"):
        assert (await client.get(path)).status_code == 404
    assert (await client.get("/auth/microsoft/link", headers=_fresh(user))).status_code == 404


# --- sign-in ---------------------------------------------------------------------


async def test_start_stores_a_flow_and_redirects_to_microsoft(client, on):
    r = await client.get("/auth/microsoft/start")
    location = urlsplit(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(location.query).items()}
    flow = session_from_response(r)["microsoft_flow"]
    assert location.netloc == "login.microsoftonline.com"
    assert location.path == "/organizations/oauth2/v2.0/authorize"
    assert q["state"] == flow["state"] and q["nonce"] == flow["nonce"] and "prompt" not in q
    assert flow["purpose"] == "signin" and len(flow["code_verifier"]) >= 43


async def test_start_sends_a_signed_in_user_home(client, db_session, on):
    user = await factories.make_user(db_session)
    r = await client.get("/auth/microsoft/start", headers=session_headers(user.id))
    assert r.headers["location"] == "/"


async def test_start_drops_a_leftover_pending_access_and_oauth_state(client, on):
    headers = raw_session_headers(
        {
            "pending_access": {"user_id": "u", "orcid": "0000-0000-0000-0001", "name": "N"},
            "oauth_state": "old",
            "post_login_redirect": "/profile",
        }
    )
    r = await client.get("/auth/microsoft/start", headers=headers)
    session = session_from_response(r)
    assert "pending_access" not in session and "oauth_state" not in session
    assert session["post_login_redirect"] == "/profile"
    assert session["microsoft_flow"]["purpose"] == "signin"


async def test_a_linked_user_signs_in(client, db_session, on, fake_idp):
    user = await factories.make_user(db_session)
    await _linked(db_session, user)
    users_before = await db_session.scalar(select(func.count()).select_from(User))
    email_before = user.email
    _token(fake_idp)
    r = await _sign_in_callback(client)
    assert r.status_code == 302 and r.headers["location"] == "/profile"
    session = session_from_response(r)
    assert session["user_id"] == str(user.id) and session["signed_in_via"] == "microsoft"
    assert session["epoch"] == 0 and int(time.time()) - session["signed_in_at"] < 60
    assert "microsoft_flow" not in session and "orcid_reauth_at" not in session
    row = await _link_row(db_session, user)
    await db_session.refresh(row)
    assert row.last_sign_in_at is not None
    await db_session.refresh(user)
    assert user.last_login_at is not None and user.email == email_before
    assert await db_session.scalar(select(func.count()).select_from(User)) == users_before


async def test_the_sign_in_response_deletes_the_impersonation_cookie(
    client, db_session, on, fake_idp
):
    user = await factories.make_user(db_session)
    await _linked(db_session, user)
    _token(fake_idp)
    headers = raw_session_headers({"microsoft_flow": _flow()})
    headers["Cookie"] += f"; copi-impersonate={uuid.uuid4()}"
    r = await client.get("/auth/microsoft/callback?code=c&state=st", headers=headers)
    deleted = [
        v
        for k, v in r.headers.multi_items()
        if k.lower() == "set-cookie" and v.startswith("copi-impersonate=")
    ]
    assert len(deleted) == 1 and "max-age=0" in deleted[0].lower()


@pytest.mark.parametrize(
    "setup, code",
    [
        ("unlinked", "microsoft_not_linked"),
        ("other_tenant", "microsoft_failed"),
        ("guest", "microsoft_failed"),
    ],
)
async def test_refused_sign_ins(client, db_session, on, fake_idp, setup, code):
    user = await factories.make_user(db_session)
    if setup != "unlinked":
        await _linked(db_session, user)
    if setup == "other_tenant":
        _token(fake_idp, tenant=OTHER_TENANT)
    elif setup == "guest":
        _token(fake_idp, acct=1)
    else:
        _token(fake_idp)
    r = await _sign_in_callback(client)
    assert r.headers["location"] == f"/login?error={code}"
    assert "user_id" not in (session_from_response(r) or {})


async def test_a_denied_account_goes_to_access_pending(client, db_session, on, fake_idp):
    user = await factories.make_user(db_session, access_status="denied")
    await _linked(db_session, user)
    _token(fake_idp)
    r = await _sign_in_callback(client)
    assert r.headers["location"] == "/access-pending"
    assert set(session_from_response(r)) == {"pending_access"}
    row = await _link_row(db_session, user)
    await db_session.refresh(row)
    assert row.last_sign_in_at is None  # a refused sign-in is not a use of the link


@pytest.mark.parametrize(
    "query, flow, code",
    [
        ("code=c&state=wrong", None, "state_mismatch"),
        ("code=c", None, "state_mismatch"),
        ("state=st", None, "state_mismatch"),
        ("code=c&state=st", "expired", "state_mismatch"),
        ("error=access_denied&state=st", None, "microsoft_cancelled"),
        ("error=server_error&state=st", None, "microsoft_error"),
    ],
)
async def test_callback_errors(client, on, query, flow, code):
    r = await _sign_in_callback(
        client, query, _flow(started_at=int(time.time()) - 601) if flow == "expired" else None
    )
    assert r.headers["location"] == f"/login?error={code}"


async def test_an_error_with_a_foreign_state_is_a_state_mismatch_and_not_logged(
    client, on, caplog
):
    with caplog.at_level(logging.DEBUG):
        r = await _sign_in_callback(client, "error=access_denied&state=other")
    assert r.headers["location"] == "/login?error=state_mismatch"
    # The app's own loggers only: httpx logs the request URL, which carries the query string.
    assert not [rec for rec in caplog.records
                if rec.name.startswith("src.") and "access_denied" in rec.getMessage()]


async def test_an_error_on_our_own_flow_is_logged_with_repr_and_a_cap(client, on, caplog):
    with caplog.at_level(logging.WARNING):
        r = await _sign_in_callback(client, "error=a%0Ab&state=st&error_description=" + "d" * 400)
    assert r.headers["location"] == "/login?error=microsoft_error"
    message = next(rec.getMessage() for rec in caplog.records if "error=" in rec.getMessage())
    assert "'a\\nb'" in message and "d" * 300 in message and "d" * 301 not in message


async def test_a_replayed_callback_is_refused(client, db_session, on, fake_idp):
    r = await _sign_in_callback(client, flow=False)
    assert r.headers["location"] == "/login?error=state_mismatch"
    user = await factories.make_user(db_session)
    await _linked(db_session, user)
    _token(fake_idp)
    first = await _sign_in_callback(client)
    assert first.headers["location"] == "/profile"
    token_requests = len(fake_idp.token_requests)
    replay = await client.get(
        "/auth/microsoft/callback?code=c&state=st",
        headers=raw_session_headers(session_from_response(first)),
    )
    assert replay.headers["location"] == "/settings?microsoft=state_mismatch"
    assert len(fake_idp.token_requests) == token_requests  # the code was never sent again


async def test_a_token_response_without_an_id_token_fails_cleanly(client, on, fake_idp):
    fake_idp.token_response = (200, {"token_type": "Bearer", "access_token": "opaque"})
    r = await _sign_in_callback(client)
    assert r.headers["location"] == "/login?error=microsoft_failed"


async def test_a_key_outage_with_a_cold_cache_fails_cleanly(client, on, fake_idp):
    _token(fake_idp)
    fake_idp.keys_status = 503
    r = await _sign_in_callback(client)
    assert r.headers["location"] == "/login?error=microsoft_failed"


async def test_interaction_required_asks_to_sign_in_again(client, on, fake_idp):
    fake_idp.token_response = (400, {"error": "interaction_required"})
    r = await _sign_in_callback(client)
    assert r.headers["location"] == "/login?error=microsoft_interaction_required"


# --- linking ---------------------------------------------------------------------


async def test_link_needs_a_fresh_orcid_sign_in(client, db_session, on):
    user = await factories.make_user(db_session)
    now = int(time.time())
    for headers in (
        session_headers(user.id),
        # an ordinary ORCID sign-in, however recent, proves no password
        session_headers(user.id, signed_in_at=now, signed_in_via="orcid"),
        session_headers(
            user.id, signed_in_at=now - 901, signed_in_via="orcid", orcid_reauth_at=now - 901
        ),
        session_headers(
            user.id, signed_in_at=now, signed_in_via="microsoft", orcid_reauth_at=now
        ),
    ):
        r = await client.get("/auth/microsoft/link", headers=headers)
        assert r.headers["location"] == "/settings?microsoft=link_refused"


async def test_link_start_asks_microsoft_for_the_account_picker(client, db_session, on):
    user = await factories.make_user(db_session)
    r = await client.get("/auth/microsoft/link", headers=_fresh(user))
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["prompt"] == ["select_account"]
    flow = session_from_response(r)["microsoft_flow"]
    assert flow["purpose"] == "link" and flow["user_id"] == str(user.id)


async def test_link_is_refused_while_impersonating(client, db_session, on):
    admin = await factories.make_user(db_session, is_admin=True)
    user = await factories.make_user(db_session)
    r = await client.get("/auth/microsoft/link", headers=_fresh(admin, impersonate=user.id))
    assert r.status_code == 403
    # a dangling cookie still marks the request as impersonating
    r = await client.get("/auth/microsoft/link", headers=_fresh(admin, impersonate=uuid.uuid4()))
    assert r.status_code == 403
    # a non-admin's cookie is ignored. A user nobody impersonated in this test: get_current_user
    # tags the impersonated ORM instance, and the test's one session would hand it back tagged.
    other = await factories.make_user(db_session)
    r = await client.get("/auth/microsoft/link", headers=_fresh(other, impersonate=admin.id))
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://login.microsoftonline.com/")


async def _link_callback(client, user, **session):
    flow = _flow("link", user_id=str(user.id))
    return await client.get(
        "/auth/microsoft/callback?code=c&state=st",
        headers=_fresh(user, microsoft_flow=flow, **session),
    )


async def test_the_link_callback_links_the_account(client, db_session, on, fake_idp):
    user = await factories.make_user(db_session)
    _token(fake_idp)
    r = await _link_callback(client, user)
    assert r.headers["location"] == "/settings?microsoft=linked"
    row = await _link_row(db_session, user)
    assert (str(row.tenant_id), str(row.object_id), row.sign_in_name) == (
        TENANT,
        OID,
        "staff@example.edu",
    )


@pytest.mark.parametrize(
    "taken_by, code",
    [("someone_else", "account_taken"), ("self", "user_already_linked")],
)
async def test_the_link_callback_refuses_a_second_link(
    client, db_session, on, fake_idp, taken_by, code, caplog
):
    user = await factories.make_user(db_session)
    if taken_by == "someone_else":
        await _linked(db_session, await factories.make_user(db_session))
    else:
        await _linked(db_session, user, oid=str(uuid.uuid4()))
    _token(fake_idp)
    with caplog.at_level(logging.INFO, logger="src.routers.microsoft_auth"):
        r = await _link_callback(client, user)
    assert r.headers["location"] == f"/settings?microsoft={code}"
    assert any(str(user.id) in m and f"tid={TENANT}" in m for m in _app_messages(caplog))


async def test_linking_a_user_deleted_meanwhile_is_refused_not_reported_as_taken(
    db_session,
):
    from src.services import microsoft_identity

    identity = microsoft_identity.MicrosoftIdentity(
        tenant_id=uuid.UUID(TENANT), object_id=uuid.UUID(OID), sign_in_name=None
    )
    # No users row for this id: the insert fails on the foreign key, not a unique constraint.
    assert await microsoft_identity.link_account(db_session, uuid.uuid4(), identity) == (
        "link_refused"
    )


async def test_a_link_removed_during_sign_in_signs_nobody_in(
    client, db_session, on, fake_idp, monkeypatch
):
    """The link is found, then removed (and the epoch bumped) before the sign-in stamps it."""
    from sqlalchemy import text

    from src.services import microsoft_identity

    user = await factories.make_user(db_session)
    await _linked(db_session, user)
    real_find = microsoft_identity.find_linked_user

    async def find_then_remove(db, identity):
        found = await real_find(db, identity)
        # Raw SQL, as a concurrent request's committed removal would look: no ORM sync.
        await db.execute(text("DELETE FROM user_microsoft_identities WHERE user_id = :u"),
                         {"u": user.id})
        await db.execute(text("UPDATE users SET session_epoch = 1 WHERE id = :u"),
                         {"u": user.id})
        return found

    monkeypatch.setattr(microsoft_identity, "find_linked_user", find_then_remove)
    _token(fake_idp)
    r = await _sign_in_callback(client)
    assert r.headers["location"] == "/login?error=microsoft_not_linked"
    assert "user_id" not in (session_from_response(r) or {})


async def test_a_link_replaced_during_sign_in_signs_nobody_in(
    client, db_session, on, fake_idp, monkeypatch
):
    """The looked-up link is removed and the user links another Microsoft account before the
    stamp: the sign-in through the old identity is refused, not stamped onto the new link."""
    from sqlalchemy import text

    from src.services import microsoft_identity

    user = await factories.make_user(db_session)
    await _linked(db_session, user)
    real_find = microsoft_identity.find_linked_user
    other_oid = str(uuid.uuid4())

    async def find_then_relink(db, identity):
        found = await real_find(db, identity)
        await db.execute(text("UPDATE user_microsoft_identities SET object_id = :o "
                              "WHERE user_id = :u"), {"o": other_oid, "u": user.id})
        return found

    monkeypatch.setattr(microsoft_identity, "find_linked_user", find_then_relink)
    _token(fake_idp)
    r = await _sign_in_callback(client)
    assert r.headers["location"] == "/login?error=microsoft_not_linked"
    stamped = await db_session.scalar(
        text("SELECT last_sign_in_at FROM user_microsoft_identities WHERE user_id = :u"),
        {"u": user.id},
    )
    assert stamped is None


async def test_a_revocation_during_sign_in_leaves_the_new_session_stale(
    client, db_session, on, fake_idp, monkeypatch
):
    """An epoch bump that commits after the lookup must not be copied into the new session."""
    from sqlalchemy import text

    from src.services import microsoft_identity

    user = await factories.make_user(db_session)
    await _linked(db_session, user)
    real_find = microsoft_identity.find_linked_user

    async def find_then_bump(db, identity):
        found = await real_find(db, identity)
        await db.execute(text("UPDATE users SET session_epoch = 1 WHERE id = :u"),
                         {"u": user.id})
        return found

    monkeypatch.setattr(microsoft_identity, "find_linked_user", find_then_bump)
    _token(fake_idp)
    r = await _sign_in_callback(client)
    session = session_from_response(r)
    assert session["user_id"] == str(user.id) and session["epoch"] == 0
    client.cookies.clear()
    # The next request reads users afresh, as its own database session would in production
    # (this test's requests share one session, whose identity map still holds epoch 0).
    db_session.expire_all()
    later = await client.get("/profile", headers=raw_session_headers(session))
    assert later.status_code == 302 and later.headers["location"].startswith("/login")


async def test_a_failed_link_exchange_goes_to_settings(client, db_session, on, fake_idp):
    user = await factories.make_user(db_session)
    fake_idp.token_response = (200, {"token_type": "Bearer", "access_token": "opaque"})
    r = await _link_callback(client, user)
    assert r.headers["location"] == "/settings?microsoft=microsoft_failed"


async def test_the_link_callback_rechecks_freshness(client, db_session, on, fake_idp):
    user = await factories.make_user(db_session)
    _token(fake_idp)
    r = await _link_callback(client, user, orcid_reauth_at=int(time.time()) - 901)
    assert r.headers["location"] == "/settings?microsoft=link_refused"
    assert await db_session.scalar(select(func.count()).select_from(UserMicrosoftIdentity)) == 0


async def test_the_link_callback_refuses_while_impersonating(
    client, db_session, on, fake_idp, caplog
):
    admin = await factories.make_user(db_session, is_admin=True)
    _token(fake_idp)
    flow = _flow("link", user_id=str(admin.id))
    with caplog.at_level(logging.INFO, logger="src.routers.microsoft_auth"):
        r = await client.get(
            "/auth/microsoft/callback?code=c&state=st",
            headers=_fresh(admin, microsoft_flow=flow, impersonate=uuid.uuid4()),
        )
    assert r.headers["location"] == "/settings?microsoft=impersonating"
    assert any(str(admin.id) in m and f"tid={TENANT}" in m for m in _app_messages(caplog))
    assert await db_session.scalar(select(func.count()).select_from(UserMicrosoftIdentity)) == 0


async def test_the_link_callback_must_be_the_session_that_started_it(
    client, db_session, on, fake_idp
):
    user = await factories.make_user(db_session)
    other = await factories.make_user(db_session)
    _token(fake_idp)
    flow = _flow("link", user_id=str(other.id))
    r = await client.get(
        "/auth/microsoft/callback?code=c&state=st", headers=_fresh(user, microsoft_flow=flow)
    )
    assert r.headers["location"] == "/settings?microsoft=link_refused"
    assert await db_session.scalar(select(func.count()).select_from(UserMicrosoftIdentity)) == 0


# --- response size ---------------------------------------------------------------


async def test_the_redirect_to_microsoft_fits_nginx_header_buffer(client, db_session, on):
    # Signed out: the longest accepted next, an invite token, a leftover refused-sign-in
    # record with the longest name and email, and an ORCID state.
    signed_out = raw_session_headers(
        {
            "post_login_redirect": "/profile?q=" + "x" * 1013,
            "pending_invite_token": "t" * 43,
            "pending_access": {
                "user_id": str(uuid.uuid4()),
                "orcid": "0000-0000-0000-0001",
                "name": "é" * 255,
                "email": "é" * 255,
            },
            "oauth_state": "s" * 43,
        }
    )
    r = await client.get("/auth/microsoft/start", headers=signed_out)
    assert r.status_code == 302
    assert _header_bytes(r) < NGINX_HEADER_BUDGET

    # Linking: a fresh session, then the account picker redirect.
    user = await factories.make_user(db_session)
    r = await client.get("/auth/microsoft/link", headers=_fresh(user))
    assert r.status_code == 302 and "microsoft_flow" in session_from_response(r)
    assert _header_bytes(r) < NGINX_HEADER_BUDGET
