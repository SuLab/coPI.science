"""B-04: the client half of the double-submit guard, pinned at source level (no JS
runner in this repo; the browser journey journey_review_double_click_stores_one
exercises it)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = (ROOT / "static" / "js" / "ui.js").read_text(encoding="utf-8")


def _guard() -> str:
    start = UI.index("// B-04:")
    return UI[start:]


def test_the_guard_listens_for_submit_on_the_document():
    assert "document.addEventListener('submit', function (event) {" in _guard()


def test_a_cancelled_submission_is_left_alone():
    assert "if (event.defaultPrevented" in _guard()


def test_only_post_forms_that_leave_the_page_are_guarded():
    guard = _guard()
    assert "(form.getAttribute('method') || 'get').toLowerCase() !== 'post'" in guard
    assert "form.hasAttribute('data-allow-resubmit')" in guard
    assert "target !== '_self'" in guard


def test_buttons_are_disabled_after_the_entry_list_is_built():
    guard = _guard()
    assert "window.setTimeout(function () {" in guard
    assert "b.disabled = true;" in guard


def test_a_page_restored_from_the_bfcache_re_enables_them():
    guard = _guard()
    assert "window.addEventListener('pageshow', function (event) {" in guard
    assert "b.disabled = false;" in guard
