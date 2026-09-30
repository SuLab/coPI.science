"""Characterization pins for the public (no-login) route surface in src/routers/public.py.

These capture CURRENT behavior — including security-hardened validation (SEC-7/16/17) —
not desired behavior. If the app changes intentionally, update the pins.
"""

import pytest

pytestmark = pytest.mark.characterization


# --- root ----------------------------------------------------------------

async def test_root_anonymous_redirects_to_login(client):
    r = await client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


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
