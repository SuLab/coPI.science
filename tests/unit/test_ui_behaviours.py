"""Inline handlers moved to static/js/ui.js (spec §6.3). There is no JS runner in
this repo, so this pins the source: the listeners exist, the templates carry no
on…= attribute, and every data attribute is wired to something that exists."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"
UI_JS = (ROOT / "static" / "js" / "ui.js").read_text(encoding="utf-8")

#: An inline event-handler attribute (or a string assigned to an on… property).
INLINE_HANDLER = re.compile(r"""(?<![\w-])on[a-z]+\s*=\s*["']""")


def _templates():
    for path in sorted(TEMPLATES.rglob("*.html")):
        yield path.relative_to(TEMPLATES).as_posix(), path.read_text(encoding="utf-8")


def test_no_template_carries_an_inline_handler():
    offenders = [
        f"{name}:{text.count(chr(10), 0, m.start()) + 1}"
        for name, text in _templates()
        for m in INLINE_HANDLER.finditer(text)
    ]
    assert offenders == []


def test_base_loads_ui_js():
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert '<script src="/static/js/ui.js" defer></script>' in base


def test_ui_js_delegates_every_behaviour_on_document():
    assert 'document.addEventListener("click"' in UI_JS
    assert 'const INTERACTIVE = "a, button, input, select, textarea, label, summary";' in UI_JS
    for needle in (
        '"[data-row-href]"',
        '"[data-toggle-target]"',
        'classList.toggle("hidden")',
    ):
        assert needle in UI_JS, needle
    assert "innerHTML" not in UI_JS
    assert "data-autosubmit" not in UI_JS and "data-filter-nav" not in UI_JS


def test_row_hrefs_are_local_paths():
    hrefs = [
        (name, m.group(1))
        for name, text in _templates()
        for m in re.finditer(r'data-row-href="([^"]*)"', text)
    ]
    assert {name for name, _ in hrefs} == {
        "admin/users.html",
        "manager/pis.html",
        "admin/activity.html",
        "manager/activity.html",
    }
    assert all(h.startswith("/") and not h.startswith("//") for _, h in hrefs)


def test_every_toggle_target_names_an_id_in_the_same_template():
    pairs = [
        (name, text, m.group(1))
        for name, text in _templates()
        for m in re.finditer(r'data-toggle-target="([^"]+)"', text)
    ]
    assert {name for name, _, _ in pairs} == {"admin/cohorts.html"}
    for name, text, target in pairs:
        assert f'id="{target}"' in text, (name, target)
