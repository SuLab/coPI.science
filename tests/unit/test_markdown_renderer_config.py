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


def test_the_factory_disables_strikethrough_for_every_profile():
    # The chat and graph build their own private instances, which the global
    # override in markdown.js never reaches; the factory gives each the override.
    factory = JS[JS.index("function createSanitizingMarked"):JS.index("window.createSanitizingMarked =")]
    assert "const tokenizer = { del() { return undefined; } };" in factory
    assert "tokenizer: tokenizer" in factory


def test_factory_defines_three_profiles():
    assert "window.createSanitizingMarked" in JS
    for profile in ('"page"', '"chat"', '"graph"'):
        assert profile in JS


def test_chat_and_graph_use_the_factory():
    assert 'createSanitizingMarked("chat")' in CHAT_JS
    assert "new window.marked.Marked(" not in CHAT_JS
    assert 'createSanitizingMarked("graph")' in CABO
    assert "marked.setOptions" not in CABO
    assert "marked.use(" not in CABO
    assert "_head_assets.html" in CABO


def test_every_profile_disables_strikethrough():
    factory = JS[JS.index("function createSanitizingMarked"):]
    assert factory.count("del(") >= 1 and "tokenizer" in factory


def test_chat_profile_keeps_its_raw_html_hardening():
    factory = JS[JS.index("function createSanitizingMarked"):JS.index("window.createSanitizingMarked =")]
    chat = factory[factory.index('profile === "chat"'):factory.index('profile === "graph"')]
    assert "tokenizer.tag = function" in chat
    assert "inRawBlock: false" in chat
    assert "html: function (html)" in chat


def test_graph_profile_keeps_gfm_and_breaks():
    factory = JS[JS.index('profile === "graph"'):JS.index("window.createSanitizingMarked =")]
    assert "ext.gfm = true;" in factory and "ext.breaks = true;" in factory
