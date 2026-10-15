"""The Help tab's routes (spec docs/specs/2026-10-14-help-and-onboarding-design.md §4, §8.4,
§10.2): the role gate, the nav, 404s for unknown slugs and steps, the tutorial redirect,
Finish while impersonating, the Microsoft section, and the screenshot route.

Setup is committed (a savepoint release inside the test's outer transaction) before each
request, because a refused route rolls the shared session back and would otherwise take
the setup with it.
"""
import json
import re
import struct
import zlib

import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI, USER_ROLE_REVIEWER
from src.web import help_pages
from tests import factories
from tests.integration.test_manager_access import auth_headers
from tests.oidc_fakes import make_config

pytestmark = pytest.mark.integration


async def _ids(db_session, *roles):
    """One committed user per role, as ids (a refused request expires ORM objects)."""
    ids = [(await factories.make_user(db_session, user_role=role)).id for role in roles]
    await db_session.commit()
    return ids


def _nav(body: str, label: str) -> str:
    match = re.search(rf'<nav aria-label="{label}".*?</nav>', body, re.S)
    assert match, f"no nav {label!r}"
    return match.group(0)


def _png_1x1() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00\x00")) + chunk(b"IEND", b""))


@pytest.fixture
def shots(monkeypatch, tmp_path):
    """A manifest with one all-audience shot ``a`` and one staff shot ``s``."""
    meta = {"alt": "A", "width": 1, "height": 1, "sources": {}, "commit": "x"}
    manifest = {"a": {"audience": "all", **meta}, "s": {"audience": "staff", **meta}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for shot_id in manifest:
        (tmp_path / f"{shot_id}.png").write_bytes(_png_1x1())
    monkeypatch.setattr(help_pages, "SHOTS_DIR", tmp_path)
    help_pages._manifest.cache_clear()
    yield
    help_pages._manifest.cache_clear()


async def test_help_index_per_role(client, db_session):
    admin, manager, reviewer, pi = await _ids(
        db_session, USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_REVIEWER, USER_ROLE_PI)
    bodies = {}
    for name, user_id in (("admin", admin), ("manager", manager), ("reviewer", reviewer)):
        r = await client.get("/help", headers=auth_headers(user_id))
        assert r.status_code == 200, (name, r.status_code)
        bodies[name] = r.text
    assert 'href="/help/managing-pis"' not in bodies["reviewer"]
    assert 'href="/help/managing-pis"' in bodies["manager"]
    r = await client.get("/help", headers=auth_headers(pi))
    assert r.status_code == 403 and "Review access required" in r.text
    r = await client.get("/help", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login?next=%2Fhelp"


async def test_head_mirrors_get_on_help(client, db_session):
    (reviewer,) = await _ids(db_session, USER_ROLE_REVIEWER)
    get = await client.get("/help", headers=auth_headers(reviewer))
    head = await client.head("/help", headers=auth_headers(reviewer))
    assert head.status_code == get.status_code == 200
    assert head.headers["content-type"] == get.headers["content-type"]
    assert head.content == b""


async def test_help_nav_link_per_role(client, db_session):
    admin, manager, reviewer, pi = await _ids(
        db_session, USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_REVIEWER, USER_ROLE_PI)
    for user_id, path in ((reviewer, "/workspace/assessments"), (manager, "/workspace/pis"),
                          (admin, "/admin/users")):
        r = await client.get(path, headers=auth_headers(user_id))
        assert r.status_code == 200, (path, r.status_code)
        assert 'href="/help"' in _nav(r.text, "Main"), path
    r = await client.get("/profile", headers=auth_headers(pi))
    assert r.status_code == 200
    assert 'href="/help"' not in _nav(r.text, "Main")

    r = await client.get("/help/assessments", headers=auth_headers(reviewer))
    assert r.status_code == 200
    help_link = re.search(r'<a href="/help" class="([^"]*)">Help</a>', _nav(r.text, "Main"))
    assert help_link and "text-indigo-600 font-semibold" in help_link.group(1)
    assert re.search(r'<a [^>]*aria-current="page"[^>]*>Assessments</a>',
                     _nav(r.text, "Help sections"))


async def test_staff_page_forbidden_to_reviewer(client, db_session):
    (reviewer,) = await _ids(db_session, USER_ROLE_REVIEWER)
    r = await client.get("/help/managing-pis", headers=auth_headers(reviewer))
    assert r.status_code == 403 and "Manager access required" in r.text
    r = await client.get("/help/tutorials/manager/1", headers=auth_headers(reviewer))
    assert r.status_code == 403


async def test_unknown_slug_and_step_404(client, db_session):
    (reviewer,) = await _ids(db_session, USER_ROLE_REVIEWER)
    for path in ("/help/nope", "/help/tutorials/nope", "/help/tutorials/reviewer/99"):
        r = await client.get(path, headers=auth_headers(reviewer), follow_redirects=False)
        assert r.status_code == 404, path


async def test_bare_tutorial_redirects_to_step_one(client, db_session):
    (reviewer,) = await _ids(db_session, USER_ROLE_REVIEWER)
    r = await client.get("/help/tutorials/reviewer", headers=auth_headers(reviewer),
                         follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].endswith("/help/tutorials/reviewer/1")


async def test_finish_is_a_link_while_impersonating(client, db_session):
    admin, reviewer = await _ids(db_session, USER_ROLE_ADMIN, USER_ROLE_REVIEWER)
    r = await client.get("/help/tutorials/reviewer/8",
                         headers=auth_headers(admin, impersonate=reviewer))
    assert r.status_code == 200
    assert "Back to Help" in r.text
    assert 'action="/help/intro/dismiss"' not in r.text


async def test_microsoft_section_follows_feature_flag(client, db_session, asgi_app):
    (reviewer,) = await _ids(db_session, USER_ROLE_REVIEWER)
    asgi_app.state.microsoft_sign_in = None
    r = await client.get("/help/getting-started", headers=auth_headers(reviewer))
    assert r.status_code == 200 and 'id="microsoft"' not in r.text
    asgi_app.state.microsoft_sign_in = make_config()
    r = await client.get("/help/getting-started", headers=auth_headers(reviewer))
    assert r.status_code == 200 and 'id="microsoft"' in r.text


async def test_shot_route_rejects_unknown_and_traversal(client, db_session, shots):
    (reviewer,) = await _ids(db_session, USER_ROLE_REVIEWER)
    for path in ("/help/shots/nope.png", "/help/shots/..%2Fmanifest.png",
                 "/help/shots/x%2F...png"):
        r = await client.get(path, headers=auth_headers(reviewer), follow_redirects=False)
        assert r.status_code == 404, path


async def test_shot_route_audience(client, db_session, shots):
    admin, manager, reviewer, pi = await _ids(
        db_session, USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_REVIEWER, USER_ROLE_PI)
    r = await client.get("/help/shots/a.png", headers=auth_headers(reviewer))
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert "private" in r.headers["cache-control"]
    r = await client.get("/help/shots/s.png", headers=auth_headers(reviewer))
    assert r.status_code == 403
    r = await client.get("/help/shots/s.png", headers=auth_headers(admin, impersonate=reviewer))
    assert r.status_code == 403
    r = await client.get("/help/shots/s.png", headers=auth_headers(manager))
    assert r.status_code == 200
    r = await client.get("/help/shots/a.png", headers=auth_headers(pi))
    assert r.status_code == 403


async def test_shot_route_anonymous_has_no_next(client, shots):
    r = await client.get("/help/shots/a.png", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"
