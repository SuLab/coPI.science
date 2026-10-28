"""The chat drawer partial rendered standalone for both subjects
(docs/specs/2026-10-27-projects-pages-redesign-design.md §8.7): the assessment
rendering keeps today's URLs and strings, the project rendering carries its own, and
the script still names no route."""

import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.web.templating import make_templates

pytestmark = pytest.mark.integration

JS = Path(__file__).resolve().parents[2] / "static" / "js" / "assessment_chat.js"
PARTIAL = "assessments/_chat_drawer.html"
NONCE = "test-nonce-123"

ASSESSMENT_STRINGS = (
    'title="Ask about this assessment" aria-label="Ask about this assessment"',
    '<span id="chat">Ask about this assessment</span>',
    "Questions about this assessment. A click sends one:",
    'placeholder="Ask about the interview or the proposal. Enter sends; Shift+Enter starts a new line."',
    "This interview may still be running. If the hub updates this verdict, earlier "
    "questions stay visible here but are no longer used as context.",
)
PROJECT_STRINGS = (
    'title="Ask about this project" aria-label="Ask about this project"',
    '<span id="chat">Ask about this project</span>',
    "Questions about this project. A click sends one:",
    'placeholder="Ask about the project, its plan or the conversation. Enter sends; '
    'Shift+Enter starts a new line."',
)
SHARED_HOOKS = (
    'id="assessment-chat"',
    "data-chat-open",
    "data-chat-bubble",
    "data-chat-resize",
    "data-chat-close",
    "data-chat-log",
    "data-chat-starters",
    "data-chat-input",
    "data-chat-counter",
    "data-chat-usage",
    "data-chat-send",
    "data-chat-clear",
    "data-chat-error",
    "data-chat-live",
    "window.ASSESSMENT_CHAT = {",
    f'<script nonce="{NONCE}">',
    '<script src="/static/js/assessment_chat.js" defer></script>',
    "Signed in as Ada Example",
    "Saved for you; administrators with database access can read it.",
)


def _render(**context) -> str:
    template = make_templates().env.get_template(PARTIAL)
    return template.render(
        request=SimpleNamespace(state=SimpleNamespace(csp_nonce=NONCE)),
        effective_user=SimpleNamespace(name="Ada Example"),
        chat_suggestions=[],
        **context,
    )


def _config(html: str) -> str:
    return html.split("window.ASSESSMENT_CHAT = {", 1)[1].split("};", 1)[0]


def test_the_assessment_rendering_is_unchanged():
    aid = uuid.uuid4()
    html = _render(a=SimpleNamespace(id=aid))
    for text in (*SHARED_HOOKS, *ASSESSMENT_STRINGS, "data-chat-verdict-notice"):
        assert text in html, text
    config = _config(html)
    assert config == (
        f'\n    historyUrl: "/assessment-chat/{aid}",\n'
        f'    askUrl: "/assessment-chat/{aid}/messages",\n'
        f'    clearUrl: "/assessment-chat/{aid}/clear",\n'
        f'    openedUrl: "/assessment-chat/{aid}/opened"\n  '
    )
    for text in ("project", "subject:", "errors:"):
        assert text not in config, text
    assert "Ask about this project" not in html
    assert "/project-chat/" not in html


def test_the_default_kind_is_the_assessment():
    aid = uuid.uuid4()
    a = SimpleNamespace(id=aid)
    assert _render(a=a) == _render(a=a, chat_kind="assessment", chat_subject_id=aid)


def test_the_project_rendering_carries_its_urls_strings_and_errors():
    pid = uuid.uuid4()
    # No `a` in the context: the project page has none, and the partial must not
    # touch it (an eager `default(a.id)` would raise UndefinedError here).
    html = _render(chat_kind="project", chat_subject_id=pid)
    for text in (*SHARED_HOOKS, *PROJECT_STRINGS):
        assert text in html, text
    config = _config(html)
    assert f'historyUrl: "/project-chat/{pid}",' in config
    assert f'askUrl: "/project-chat/{pid}/messages",' in config
    assert f'clearUrl: "/project-chat/{pid}/clear",' in config
    assert f'openedUrl: "/project-chat/{pid}/opened",' in config
    assert 'subject: "project",' in config
    assert 'disabled: "The project chat is switched off.",' in config
    assert 'not_found: "This project no longer exists.",' in config
    assert (
        'project_open: "This project is still in progress; the chat opens once it finishes."'
        in config
    )
    # No verdict notice, no assessment wording, no assessment route.
    assert "data-chat-verdict-notice" not in html
    for text in ASSESSMENT_STRINGS:
        assert text not in html, text
    assert "/assessment-chat/" not in html
    assert "verdict" not in html.lower()


def test_the_project_drawer_has_no_details_and_no_form():
    html = _render(chat_kind="project", chat_subject_id=uuid.uuid4())
    opening = re.search(r'<aside[^>]*id="assessment-chat"[^>]*>', html)
    assert opening is not None
    drawer = html[opening.end():].split("</aside>", 1)[0]
    assert "<details" not in drawer
    assert "<form" not in drawer


def test_the_chat_script_names_neither_chat_route():
    js = JS.read_text(encoding="utf-8")
    assert "/project-chat" not in js
    assert "/assessment-chat" not in js
    assert not re.search(r"""["']/[A-Za-z]""", js)


def test_the_chat_script_takes_the_partials_errors_and_subject():
    js = JS.read_text(encoding="utf-8")
    assert "project_open:" in js
    assert "ERRORS[code] = cfg.errors[code];" in js
    assert 'const IS_PROJECT = cfg.subject === "project";' in js
    # A project never shows the verdict divider or the verdict notice.
    assert "const revisionStart = !IS_PROJECT && " in js
    assert "els.notice.hidden = IS_PROJECT || " in js
