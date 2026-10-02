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
