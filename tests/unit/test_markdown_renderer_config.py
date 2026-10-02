"""Both markdown renderers must disable GFM strikethrough: the hub writes '~' for
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


def test_the_factory_disables_strikethrough_for_every_profile():
    # The chat builds its own private instance, which the global override in
    # markdown.js never reaches; the factory gives it the override.
    factory = JS[JS.index("function createSanitizingMarked"):JS.index("window.createSanitizingMarked =")]
    assert "const tokenizer = { del() { return undefined; } };" in factory
    assert "tokenizer: tokenizer" in factory


def test_factory_defines_the_page_and_chat_profiles_only():
    assert "window.createSanitizingMarked" in JS
    for profile in ('"page"', '"chat"'):
        assert profile in JS
    assert '"graph"' not in JS


def test_chat_uses_the_factory():
    assert 'createSanitizingMarked("chat")' in CHAT_JS
    assert "new window.marked.Marked(" not in CHAT_JS


def test_every_profile_disables_strikethrough():
    factory = JS[JS.index("function createSanitizingMarked"):]
    assert factory.count("del(") >= 1 and "tokenizer" in factory


def test_chat_profile_keeps_its_raw_html_hardening():
    factory = JS[JS.index("function createSanitizingMarked"):JS.index("window.createSanitizingMarked =")]
    start = factory.index('profile === "chat"')
    chat = factory[start:factory.index("} else", start)]
    # Phase 0 Task 0-4 shares the raw-block-free tag tokenizer with the page profile.
    assert "tokenizer.tag = rawTagTokenizer" in chat
    assert "html: function (html)" in chat
    tokenizer = JS[JS.index("function rawTagTokenizer"):JS.index("function createSanitizingMarked")]
    assert "inRawBlock: false" in tokenizer
