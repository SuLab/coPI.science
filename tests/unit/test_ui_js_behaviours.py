"""Source pins for static/js/ui.js behaviours added in Phase 2 (no JS runner in CI)."""

from pathlib import Path

UI_JS = (Path(__file__).resolve().parents[2] / "static/js/ui.js").read_text(encoding="utf-8")


def test_ui_js_lazy_fragment_rejects_redirects_and_non_html():
    block = UI_JS[UI_JS.index("data-lazy-fragment"):]
    assert 'document.addEventListener("toggle"' in UI_JS, "toggle does not bubble: a capture listener"
    assert "resp.redirected" in block, "an expired session must not inject the login page"
    assert 'indexOf("text/html") !== 0' in block
    assert "slot.dataset.loaded" in block, "loads once per row"
