"""D-14: the conversations page's thread expander must not inject the login page
(a redirected fetch) or any non-HTML body, and must mark the link expanded when it
shows an error. Source-level pins; journey_thread_fetch_after_session_expiry runs it."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = (ROOT / "templates" / "agent" / "conversations.html").read_text(encoding="utf-8")


def _catch_block() -> str:
    start = SRC.index(".catch(function(err)")
    return SRC[start:SRC.index(".finally(", start)]


def test_a_redirected_response_is_rejected():
    assert "if (r.redirected) { throw new Error('session'); }" in SRC


def test_a_non_html_response_is_rejected():
    assert "type.indexOf('text/html') !== 0" in SRC


def test_the_error_path_marks_the_link_expanded():
    assert "link.setAttribute('aria-expanded', 'true');" in _catch_block()


def test_the_error_path_says_the_session_ended():
    assert "Your session has ended — reload the page." in _catch_block()


def test_the_error_text_is_set_as_text():
    block = _catch_block()
    assert "p.textContent = text;" in block
    assert "innerHTML = '<" not in block
