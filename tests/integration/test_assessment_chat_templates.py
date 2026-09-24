"""The drawer on both detail pages (spec §8): rendered for every role that may ask,
anchored where the record's citations point, and absent wherever it would be a
dead control."""

import re
from pathlib import Path

import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_REVIEWER
from tests import factories
from tests.assessment_chat_support import seed_interview
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

JS = Path(__file__).resolve().parents[2] / "static" / "js" / "assessment_chat.js"


def _main(html: str) -> str:
    return html.split("<main", 1)[1].split("</main>", 1)[0]


async def _page(client, db_session, role, surface):
    seeded = await seed_interview(db_session)
    user = await factories.make_user(db_session, user_role=role)
    resp = await client.get(
        f"/{surface}/assessments/{seeded.assessment_id}", headers=auth_headers(user.id)
    )
    assert resp.status_code == 200
    return seeded, user, _main(resp.text)


@pytest.mark.parametrize(
    "role,surface",
    [(USER_ROLE_ADMIN, "admin"), (USER_ROLE_MANAGER, "manager"), (USER_ROLE_REVIEWER, "manager")],
)
async def test_the_detail_page_renders_the_drawer_and_its_anchors(client, db_session, role, surface):
    seeded, user, body = await _page(client, db_session, role, surface)
    aid = seeded.assessment_id
    assert "data-chat-open" in body
    assert 'id="assessment-chat"' in body
    assert f'historyUrl: "/assessment-chat/{aid}"' in body
    assert f'askUrl: "/assessment-chat/{aid}/messages"' in body
    assert f'clearUrl: "/assessment-chat/{aid}/clear"' in body
    assert '<script src="/static/js/assessment_chat.js" defer></script>' in body
    assert f"Signed in as {user.name}" in body
    assert "Saved for you; administrators with database access can read it." in body
    for anchor in ("verdict", "red-flags", f"m-{seeded.message_ids[0]}", "consult-1"):
        assert f'id="{anchor}"' in body, anchor


async def test_impersonation_renders_text_instead_of_a_control(client, db_session):
    seeded = await seed_interview(db_session)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    headers = auth_headers(admin.id)
    headers["Cookie"] += f"; copi-impersonate={manager.id}"
    resp = await client.get(f"/manager/assessments/{seeded.assessment_id}", headers=headers)
    body = _main(resp.text)
    assert "Chat unavailable while impersonating" in body
    assert "data-chat-open" not in body
    assert 'id="assessment-chat"' not in body
    assert "window.ASSESSMENT_CHAT" not in body


async def test_a_disabled_chat_renders_nothing(asgi_app, client, db_session):
    asgi_app.state.assessment_chat_enabled = False
    _, _, body = await _page(client, db_session, USER_ROLE_ADMIN, "admin")
    assert "data-chat-open" not in body
    assert 'id="assessment-chat"' not in body
    assert "Chat unavailable" not in body


async def test_the_drawer_has_no_details_and_no_form(client, db_session):
    _, _, body = await _page(client, db_session, USER_ROLE_ADMIN, "admin")
    opening = re.search(r'<aside[^>]*id="assessment-chat"[^>]*>', body)
    assert opening is not None
    assert "ph-no-capture" in opening.group() and "print:hidden" in opening.group()
    drawer = body[opening.end():].split("</aside>", 1)[0]
    assert "<details" not in drawer
    assert "<form" not in drawer


def test_the_chat_script_writes_markup_only_from_sanitized_output():
    js = JS.read_text(encoding="utf-8")
    assert js.count(".innerHTML") == 1
    assert "container.innerHTML = window.DOMPurify.sanitize(" in js
    assert "ALLOWED_URI_REGEXP: /^https:/i" in js
    allowed_tags = js.split("ALLOWED_TAGS:", 1)[1].split("]", 1)[0]
    for tag in ("img", "svg", "math", "iframe", "form", "input", "style", "script"):
        assert f'"{tag}"' not in allowed_tags, tag
    assert "textContent" in js


def test_the_chat_script_carries_the_b1_and_sec_fixes():
    js = JS.read_text(encoding="utf-8")
    # B1: the thin-space citation gap, used inside markdownFor.
    assert "const MARK_GAP = String.fromCharCode(0x2009);" in js
    markdown_for = js.split("function markdownFor(turn)", 1)[1].split("\n  }\n", 1)[0]
    assert "MARK_GAP" in markdown_for
    # SEC-1a: never decode a candidate href to match it against allowed_links.
    assert "decodeURI(" not in js
    # SEC-1b: raw HTML is escaped as text by a private marked instance, never
    # passed through the global renderer.
    assert "new window.marked.Marked()" in js
    assert "html: function (html)" in js
    # New error codes the client must be able to show.
    assert '"forbidden"' not in js  # the key is unquoted object-literal style
    assert "forbidden:" in js
    assert "unexpected_stop:" in js
    # SW-7: Safari's IME-confirming Enter.
    assert "event.keyCode === 229" in js
    # SW-1: reopening (or a second opener click) while already open is a no-op.
    open_drawer = js.split("function openDrawer(opener)", 1)[1].split("\n  }\n", 1)[0]
    assert "if (state.open) {" in open_drawer


def test_the_chat_script_accepts_only_its_own_citation_markers():
    js = JS.read_text(encoding="utf-8")
    # RSEC-1/RSEC-2: a real marker carries a per-page random nonce the model never
    # sees; anything else private-use in the rendered text is dropped.
    assert "window.crypto.getRandomValues(" in js
    assert "MARK_OPEN + MARK_NONCE + String(n) + MARK_CLOSE" in js
    place = js.split("function placeCitations(root, turnKey)", 1)[1].split("\n  }\n", 1)[0]
    assert "markerNumber(part)" in place
    assert "PRIVATE_USE_ALL" in place
    # RSEC-1: the private marked instance never enters marked's raw-block state.
    assert "inRawBlock: false" in js
    # R2SEC-2: no attribute but href survives sanitizing, so a marker cannot hide
    # in a title tooltip.
    assert 'ALLOWED_ATTR: ["href"],' in js
    # RSEC-3: the lock's bounded wait has a message.
    assert "busy:" in js
    # RS-5/RS-7: a stale failure paints nothing; a terminal refusal stops polling.
    load = js.split("async function loadHistory()", 1)[1].split("\n  }\n", 1)[0]
    assert "if (stale()) {" in load
    assert "TERMINAL_CODES[code]" in load


def test_the_chat_script_names_no_route():
    """URLs come from window.ASSESSMENT_CHAT only; a path literal here would be a
    second, unchecked copy of the routes."""
    js = JS.read_text(encoding="utf-8")
    assert not re.search(r"""["']/[A-Za-z]""", js)
