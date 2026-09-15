"""Page chrome around the assessment detail body: the skip link in base.html
and the per-wrapper CSS (prose scale, focus visibility, print rules).

These assertions live apart from test_assessment_detail_page.py because they
are about the WRAPPERS (templates/{admin,manager}/assessment_detail.html) and
the shared base layout, not about the included body — and the two wrappers
duplicate their `<style>` block deliberately, so each rule is asserted on both
rendered surfaces rather than on one.
"""

from __future__ import annotations

import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER
from tests import factories
from tests.integration.test_assessment_detail_page import _seed
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

SKIP_LINK_HREF = 'href="#main-content"'


async def _both_surfaces(client, db_session) -> list[str]:
    """The admin and the manager rendering of the same assessment."""
    _, assessment = await _seed(db_session)
    admin = await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, email="chrome-admin@example.org"
    )
    manager = await factories.make_user(
        db_session, user_role=USER_ROLE_MANAGER, email="chrome-manager@example.org"
    )
    out = []
    for path, user in (
        (f"/admin/assessments/{assessment.id}", admin),
        (f"/manager/assessments/{assessment.id}", manager),
    ):
        resp = await client.get(path, headers=auth_headers(user.id))
        assert resp.status_code == 200, path
        out.append(resp.text)
    return out


async def test_the_skip_link_is_on_the_login_page_and_is_hidden_until_focused(client):
    r = await client.get("/login")
    assert r.status_code == 200
    html = r.text
    assert SKIP_LINK_HREF in html
    assert "Skip to main content" in html
    # Visually hidden until it takes focus: sr-only, unhidden by focus:not-sr-only.
    assert "sr-only" in html and "focus:not-sr-only" in html
    assert 'id="main-content"' in html


async def test_the_skip_link_and_main_landmark_are_on_the_detail_pages(
    client, db_session
):
    for html in await _both_surfaces(client, db_session):
        assert SKIP_LINK_HREF in html
        assert 'id="main-content"' in html


async def test_both_wrappers_set_the_readable_prose_scale(client, db_session):
    for html in await _both_surfaces(client, db_session):
        assert "font-size: 1.0625rem" in html
        assert "max-width: 68ch" in html
        assert "line-height: 1.6" in html
        assert "text-wrap: pretty" in html
        # The brief's pitch column is narrower than 68ch already.
        assert ".assessment-brief-pitch .assessment-prose" in html
        # The hub headline is content, not a section heading (2026-09-15 audit M5).
        assert "p.assessment-headline" in html
        assert "text-wrap: balance" in html
        # The old 65ch/1rem scale is gone.
        assert "max-width: 65ch" not in html


async def test_both_wrappers_make_keyboard_focus_visible(client, db_session):
    for html in await _both_surfaces(client, db_session):
        assert "a:focus-visible" in html
        assert "summary:focus-visible" in html
        assert "outline: 2px solid #4338ca" in html


async def test_both_wrappers_carry_print_rules_and_open_details_for_print(
    client, db_session
):
    for html in await _both_surfaces(client, db_session):
        assert "@media print" in html
        assert ".assessment-jump-nav" in html
        # CSS cannot force a <details> open, so the script has to.
        assert "beforeprint" in html
        assert "afterprint" in html


async def test_both_wrappers_use_a_link_coloured_back_link(client, db_session):
    for html in await _both_surfaces(client, db_session):
        assert 'class="text-sm text-indigo-700 hover:underline"' in html
        assert 'class="text-sm text-gray-600 hover:text-gray-700"' not in html
