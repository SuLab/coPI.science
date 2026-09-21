"""Page chrome around the assessment LIST wrappers: the sanitizing markdown
scripts, the reading-scale/print style block, and select/label wiring.

Modeled on tests/integration/test_assessment_detail_chrome.py — these
assertions live apart from test_assessment_queue_controls.py because they are
about the WRAPPERS (templates/{admin,manager}/assessments.html), copied
verbatim from the two detail wrappers, not about the shared body's data or
behaviour.
"""

from __future__ import annotations

import re

import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER
from tests import factories
from tests.integration.test_assessment_queue_controls import _seed
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration


async def _both_surfaces(client, db_session) -> list[str]:
    """The admin and the manager rendering of the same run's assessment list."""
    run = await _seed(db_session)
    admin = await factories.make_user(
        db_session, user_role=USER_ROLE_ADMIN, email="list-chrome-admin@example.org"
    )
    manager = await factories.make_user(
        db_session,
        user_role=USER_ROLE_MANAGER,
        email="list-chrome-manager@example.org",
    )
    out = []
    for base, user in (("/admin", admin), ("/manager", manager)):
        resp = await client.get(
            f"{base}/assessments?run_id={run.id}", headers=auth_headers(user.id)
        )
        assert resp.status_code == 200, base
        out.append(resp.text)
    return out


async def test_both_list_pages_carry_the_sanitizing_markdown_scripts(
    client, db_session
):
    for html in await _both_surfaces(client, db_session):
        assert (
            'src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"' in html
        )
        assert (
            "integrity=\"sha384-/TQbtLCAerC3jgaim+N78RZSDYV7ryeoBCVqTuzRrFec2akfBkHS7ACQ3PQhvMVi\""
            in html
        )
        assert (
            'src="https://cdn.jsdelivr.net/npm/dompurify@3.1.6/dist/purify.min.js"'
            in html
        )
        assert (
            "integrity=\"sha384-+VfUPEb0PdtChMwmBcBmykRMDd+v6D/oFmB3rZM/puCMDYcIvF968OimRh4KQY9a\""
            in html
        )
        assert 'src="/static/js/markdown.js"' in html


async def test_both_list_pages_set_the_readable_prose_scale(client, db_session):
    for html in await _both_surfaces(client, db_session):
        assert "font-size: 1.0625rem" in html
        assert "max-width: 68ch" in html


async def test_both_list_pages_style_prose_links(client, db_session):
    for html in await _both_surfaces(client, db_session):
        assert ".citation-link" in html, (
            "the citation link rule must be in BOTH list wrappers; no test compares "
            "the two wrappers to each other, so a one-sided edit passes everything else"
        )


async def test_both_list_pages_make_keyboard_focus_visible(client, db_session):
    for html in await _both_surfaces(client, db_session):
        assert "a:focus-visible" in html
        assert "summary:focus-visible" in html
        assert "outline: 2px solid #4338ca" in html


async def test_both_list_pages_carry_print_rules(client, db_session):
    for html in await _both_surfaces(client, db_session):
        assert "@media print" in html


async def test_the_manager_list_page_never_links_into_admin(client, db_session):
    _, manager_html = await _both_surfaces(client, db_session)
    assert "/admin/" not in manager_html


_SELECT_RE = re.compile(r'<select\b[^>]*>', re.IGNORECASE)
_ID_RE = re.compile(r'\bid="([^"]+)"')
_LABEL_FOR_RE = re.compile(r'<label\b[^>]*\bfor="([^"]+)"', re.IGNORECASE)


async def test_every_select_on_both_list_pages_has_a_labelled_id(client, db_session):
    for html in await _both_surfaces(client, db_session):
        select_ids = set()
        for tag in _SELECT_RE.findall(html):
            match = _ID_RE.search(tag)
            assert match, f"<select> without an id: {tag}"
            select_ids.add(match.group(1))
        label_fors = set(_LABEL_FOR_RE.findall(html))
        missing = select_ids - label_fors
        assert not missing, f"selects with no matching <label for>: {missing}"
