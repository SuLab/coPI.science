"""Source pins for the two security-relevant scripts (spec 2026-10-01 §5.1, §5.2).
This repo has no JS runner; the browser harness (tests/e2e/ui_audit) exercises them."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIRM = ROOT / "static" / "js" / "confirm.js"


def test_confirm_js_reads_the_message_from_an_attribute_and_never_evaluates_it():
    js = CONFIRM.read_text()
    assert 'addEventListener("submit"' in js
    assert 'getAttribute("data-confirm")' in js
    assert "window.confirm(message)" in js
    assert "event.preventDefault()" in js
    assert re.search(r"\beval\(|new Function|setTimeout\(\s*['\"]", js) is None


def test_no_template_builds_a_confirm_in_an_inline_handler():
    offenders = [
        str(p.relative_to(ROOT)) for p in (ROOT / "templates").rglob("*.html")
        if re.search(r"on(submit|click)=\"[^\"]*confirm\(", p.read_text())
    ]
    assert offenders == []


MARKDOWN = ROOT / "static" / "js" / "markdown.js"


def _page_profile(js: str) -> str:
    start = js.index('profile === "page"')
    # "} else" (not "} else if"): Phase 1 Task 1A-2 deletes the graph branch, after
    # which the page branch is followed by the final "} else {".
    return js[start: js.index("} else", start + 1)]


def test_page_profile_escapes_raw_html_except_a_lone_br():
    js = MARKDOWN.read_text()
    page = _page_profile(js)
    assert "tokenizer.tag = rawTagTokenizer" in page
    assert "LONE_BR.test" in page and "escapeHtml(" in page
    assert r"var LONE_BR = /^<br\s*\/?>$/i;" in js


def test_page_profile_renders_images_as_links():
    page = _page_profile(MARKDOWN.read_text())
    assert "image: function (href, title, text)" in page
    assert "<img" not in page


def test_render_markdown_uses_the_explicit_page_allowlist():
    js = MARKDOWN.read_text()
    assert "DOMPurify.sanitize(pageMarked.parse(md), PAGE_PURIFY)" in js
    assert "DOMPurify.sanitize(marked.parse(md))" not in js
    block = js[js.index("var PAGE_PURIFY"): js.index("};", js.index("var PAGE_PURIFY"))]
    for forbidden in ('"form"', '"input"', '"button"', '"img"', '"style"', '"iframe"',
                      '"select"', '"textarea"'):
        assert forbidden not in block
    assert 'ALLOWED_ATTR: ["href", "title", "start", "align"]' in block
    assert "ALLOW_DATA_ATTR: false" in block and "ALLOW_ARIA_ATTR: false" in block
    assert 'ADD_URI_SAFE_ATTR: ["start", "align"]' in block
    assert r"ALLOWED_URI_REGEXP: /^(?:https?:|mailto:|#)/i" in block


def test_page_profile_keeps_task_list_state_and_text_after_a_leading_br():
    page = _page_profile(MARKDOWN.read_text())
    assert 'checkbox: function (checked) { return checked ? "[x]" : "[ ]"; }' in page
    assert "pageMarked.parse(lead[1])" in page
