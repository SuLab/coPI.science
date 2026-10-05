"""Source pins for static/js/assessment_chat.js Phase 2 behaviours (B-08, B-16, B-17,
B-20). Browser behaviour is exercised by tests/e2e/ui_audit/journeys_phase2_2b.py."""

from pathlib import Path

JS = (Path(__file__).resolve().parents[2] / "static/js/assessment_chat.js").read_text(encoding="utf-8")


def _body(signature: str) -> str:
    start = JS.index(signature)
    return JS[start:JS.index("\n  }\n", start)]


def test_polls_ask_for_the_sweepless_history():
    assert 'cfg.historyUrl + "?poll=1"' in _body("async function loadHistory(isPoll)")
    assert "loadHistory(true)" in _body("function schedulePoll()")


def test_modality_is_re_evaluated_on_resize():
    assert "function applyModality()" in JS
    assert 'WIDE.addEventListener("change"' in JS
    assert "applyModality();" in _body("function openDrawer(opener, via)")


def test_render_keeps_scroll_position_and_focus():
    body = _body("function render()")
    assert "const pinned = " in body
    assert "log.scrollTop = pinned ? log.scrollHeight : keptTop;" in body
    assert "again.focus({ preventScroll: true })" in body


def test_show_in_page_keeps_the_target_clear_of_the_drawer():
    assert "keepClearOfDrawer(target);" in _body("function showInPage(anchor)")
    clear = _body("function keepClearOfDrawer(target)")
    assert "WIDTH_MIN_PX" in clear and "closeDrawer();" in clear
    assert '"chat-src-show-" + turnKey' in JS
