"""All three markdown renderers must disable GFM strikethrough: the hub writes '~' for
'approximately' (e.g. '~30-37%'), and marked pairs tildes into <del> spans.
See docs/plans/2026-09-04-assessment-ui-and-proposal-limit-design.md, Feature 1."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "static" / "js" / "markdown.js").read_text()


def test_strikethrough_tokenizer_is_disabled():
    # The one durable signal that tildes are treated as literal text.
    assert "marked.use(" in JS
    assert "del(" in JS  # the tokenizer being overridden off


CHAT_JS = (ROOT / "static" / "js" / "assessment_chat.js").read_text()
CABO = (ROOT / "templates" / "cabo_graph.html").read_text()


def test_the_chat_renderer_disables_strikethrough():
    # The chat builds its own `new Marked()` instance, which the global
    # override in markdown.js never reaches.
    body = CHAT_JS[CHAT_JS.index("function getSanitizingMarked"):CHAT_JS.index("function renderBody")]
    assert "del: function" in body
    assert "return undefined;" in body[body.index("del: function"):]


def test_the_graph_page_disables_strikethrough_before_configuring_marked():
    override = CABO.index("marked.use({ tokenizer: { del() { return undefined; } } })")
    assert override < CABO.index("marked.setOptions({ gfm: true, breaks: true })")
