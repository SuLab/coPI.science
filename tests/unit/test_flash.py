"""src/web/flash.py: queueing, draining, bounds, and the lazy context processor."""
import pytest
from starlette.middleware.sessions import Session
from starlette.requests import Request

from src.web.flash import (
    FLASH_SESSION_KEY,
    MAX_FLASH_CHARS,
    MAX_FLASHES,
    flash,
    flash_context,
    pop_flashes,
)


def _request(session=None) -> Request:
    scope = {"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}
    if session is not None:
        scope["session"] = session
    return Request(scope)


def test_flash_queues_in_order_and_pop_drains():
    request = _request(Session())
    flash(request, "first")
    flash(request, "second", "error")
    assert pop_flashes(request) == [
        {"text": "first", "kind": "info"},
        {"text": "second", "kind": "error"},
    ]
    assert pop_flashes(request) == []


def test_flash_marks_the_session_modified():
    """Starlette re-sends the cookie only for a modified session."""
    session = Session()
    flash(_request(session), "saved", "success")
    assert session.modified


def test_text_is_capped_and_the_queue_is_bounded():
    request = _request(Session())
    flash(request, "x" * (MAX_FLASH_CHARS + 50))
    assert len(pop_flashes(request)[0]["text"]) == MAX_FLASH_CHARS
    for n in range(MAX_FLASHES + 2):
        flash(request, f"m{n}")
    queue = pop_flashes(request)
    assert len(queue) == MAX_FLASHES
    assert queue[-1]["text"] == f"m{MAX_FLASHES + 1}"


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        flash(_request(Session()), "x", "warning")


def test_pop_without_a_session_is_empty():
    assert pop_flashes(_request(None)) == []


def test_context_processor_pops_only_when_called():
    """A partial rendered through TemplateResponse runs the processor too; only
    base.html's call to get_flashes() may drain the queue."""
    session = Session()
    request = _request(session)
    flash(request, "kept for the next page")
    context = flash_context(request)
    assert FLASH_SESSION_KEY in session
    assert context["get_flashes"]() == [{"text": "kept for the next page", "kind": "info"}]
    assert FLASH_SESSION_KEY not in session
