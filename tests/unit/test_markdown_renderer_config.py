"""markdown.js must disable GFM strikethrough: the hub writes '~' for
'approximately' (e.g. '~30-37%'), and marked pairs tildes into <del> spans.
See docs/plans/2026-09-04-assessment-ui-and-proposal-limit-design.md, Feature 1."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = (ROOT / "static" / "js" / "markdown.js").read_text()


def test_strikethrough_tokenizer_is_disabled():
    # The one durable signal that tildes are treated as literal text.
    assert "marked.use(" in JS
    assert "del(" in JS  # the tokenizer being overridden off
