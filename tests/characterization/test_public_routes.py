"""Characterization pins for the public (no-login) route surface in src/routers/public.py.

These capture CURRENT behavior — including security-hardened validation (SEC-7/16/17) —
not desired behavior. If the app changes intentionally, update the pins.
"""

import uuid

import pytest

from tests import factories

pytestmark = pytest.mark.characterization


# --- landing -----------------------------------------------------------------

async def test_landing_anonymous_200_html(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


# --- waitlist (POST /waitlist) ----------------------------------------------

async def test_waitlist_valid_email_succeeds(client):
    r = await client.post("/waitlist", data={"email": "pin-valid@example.edu"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


async def test_waitlist_invalid_email_400(client):
    r = await client.post("/waitlist", data={"email": "not-an-email"})
    assert r.status_code == 400


async def test_waitlist_missing_email_422(client):
    # email is Form(...) — required; FastAPI rejects the missing field.
    r = await client.post("/waitlist", data={"name": "No Email"})
    assert r.status_code == 422


async def test_waitlist_oversized_fields_truncated_not_500(client):
    # SEC-17: name/institution/note are truncated to column limits before the
    # DB write, so oversized input returns 200 instead of a 500 DataError.
    r = await client.post(
        "/waitlist",
        data={
            "email": "pin-oversize@example.edu",
            "name": "N" * 5000,
            "institution": "I" * 5000,
            "note": "X" * 50000,
        },
    )
    assert r.status_code == 200


# --- access-pending ----------------------------------------------------------

async def test_access_pending_get_200(client):
    r = await client.get("/access-pending")
    assert r.status_code == 200


async def test_access_pending_email_without_session_redirects_home(client):
    # No pending_access in session -> redirect to "/".
    r = await client.post("/access-pending/email", data={"email": "x@example.edu"})
    assert r.status_code == 302
    assert r.headers["location"] == "/"


# --- public collaboration graphs (DB-only, no network) ----------------------

@pytest.mark.parametrize(
    "path",
    ["/cabo-graph", "/scripps-graph", "/schultz-alumni-pilot", "/schultz-group-alumni"],
)
async def test_graph_routes_render_200_with_csp(client, path):
    r = await client.get(path)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    # _render_graph attaches a per-response CSP header.
    assert "content-security-policy" in {k.lower() for k in r.headers.keys()}


# --- proposal vote (POST /api/proposal-vote) --------------------------------

async def test_proposal_vote_missing_token_422(client):
    # SEC-7: a browser token is required (NULL tokens can't dedup -> unbounded rows).
    body = {"decision_id": str(uuid.uuid4()), "vote": "up"}
    r = await client.post("/api/proposal-vote", json=body)
    assert r.status_code == 422


async def test_proposal_vote_bad_vote_value_422(client):
    body = {"decision_id": str(uuid.uuid4()), "vote": "sideways", "voter_token": "tok"}
    r = await client.post("/api/proposal-vote", json=body)
    assert r.status_code == 422


async def test_proposal_vote_unknown_decision_404(client):
    body = {"decision_id": str(uuid.uuid4()), "vote": "up", "voter_token": "tok"}
    r = await client.post("/api/proposal-vote", json=body)
    assert r.status_code == 404


async def test_proposal_vote_on_private_decision_404(client, db_session):
    # Votes are only accepted on public proposals; a collab_private origin is
    # treated as unknown (does not confirm existence).
    d = await factories.make_thread_decision(
        db_session, outcome="proposal", origin_visibility="collab_private"
    )
    body = {"decision_id": str(d.id), "vote": "up", "voter_token": "tok"}
    r = await client.post("/api/proposal-vote", json=body)
    assert r.status_code == 404


async def test_proposal_vote_happy_path_returns_id(client, db_session):
    d = await factories.make_thread_decision(
        db_session, outcome="proposal", origin_visibility="public"
    )
    body = {"decision_id": str(d.id), "vote": "up", "voter_token": "browser-tok-1"}
    r = await client.post("/api/proposal-vote", json=body)
    assert r.status_code == 200
    vote_id = r.json()["id"]
    assert uuid.UUID(vote_id)  # parses


async def test_proposal_vote_revote_updates_same_row(client, db_session):
    d = await factories.make_thread_decision(
        db_session, outcome="proposal", origin_visibility="public"
    )
    body = {"decision_id": str(d.id), "vote": "up", "voter_token": "browser-tok-2"}
    r1 = await client.post("/api/proposal-vote", json=body)
    body["vote"] = "down"
    r2 = await client.post("/api/proposal-vote", json=body)
    assert r1.json()["id"] == r2.json()["id"]  # one row per (decision, token)


# --- proposal vote details (POST /api/proposal-vote/{id}/details) -----------

async def test_proposal_vote_details_unknown_vote_404(client):
    r = await client.post(
        f"/api/proposal-vote/{uuid.uuid4()}/details",
        json={"details": "nice"},
    )
    assert r.status_code == 404


async def test_proposal_vote_details_happy_path_ok(client, db_session):
    d = await factories.make_thread_decision(
        db_session, outcome="proposal", origin_visibility="public"
    )
    created = await client.post(
        "/api/proposal-vote",
        json={"decision_id": str(d.id), "vote": "up", "voter_token": "browser-tok-3"},
    )
    vote_id = created.json()["id"]
    r = await client.post(
        f"/api/proposal-vote/{vote_id}/details",
        json={"details": "more context", "voter_token": "browser-tok-3"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}


@pytest.mark.parametrize("bad_payload", [{}, {"voter_token": "not-the-owner"}])
async def test_proposal_vote_details_wrong_or_absent_token_403(
    client, db_session, bad_payload
):
    """SEC: `if vote_obj.voter_token and token and vote_obj.voter_token != token`
    short-circuited to False whenever the caller simply omitted `voter_token`
    from the body (token is None) — so anyone holding a vote_id could overwrite,
    or erase (`details: null`), another visitor's free-text comment with no
    token at all. A wrong token, and an altogether absent one, must both be
    rejected whenever the stored row actually has a token."""
    d = await factories.make_thread_decision(
        db_session, outcome="proposal", origin_visibility="public"
    )
    created = await client.post(
        "/api/proposal-vote",
        json={"decision_id": str(d.id), "vote": "up", "voter_token": "browser-tok-4"},
    )
    vote_id = created.json()["id"]
    r = await client.post(
        f"/api/proposal-vote/{vote_id}/details",
        json={"details": "attacker-supplied", **bad_payload},
    )
    assert r.status_code == 403

    # The original comment/attempted tamper must not have landed: the correct
    # token still works and is the only way to update this row.
    r2 = await client.post(
        f"/api/proposal-vote/{vote_id}/details",
        json={"details": "legit update", "voter_token": "browser-tok-4"},
    )
    assert r2.status_code == 200
    assert r2.json() == {"ok": True}


# --- interactive docs (E1.4) -------------------------------------------------

@pytest.mark.parametrize(
    "path", ["/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"]
)
async def test_the_openapi_schema_is_not_public(client, path):
    """FastAPI's default doc surface is an unauthenticated route inventory.

    ``create_app()`` used to pass no ``docs_url``/``redoc_url``/``openapi_url``,
    so all four of these were served to anyone: between them they publish every
    path, every method and every form field name in the app — the
    reconnaissance half of a CSRF attack. They are now switched off at the
    application factory, so the routes are not registered at all: the assertion
    is 404 (no such route), not 401/403 (route exists, needs auth).

    ``app.openapi()`` still works in-process, which is what
    tests/unit/test_reachability.py's route walk uses.
    """
    r = await client.get(path)
    assert r.status_code == 404, f"{path} is still served publicly ({r.status_code})"
