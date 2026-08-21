import pytest
from starlette.requests import Request

from src.main import AgentBadgeMiddleware


def _request(path: str, user_id: str | None = None) -> Request:
    scope = {
        "type": "http", "method": "GET", "path": path, "headers": [],
        "query_string": b"",
    }
    if user_id is not None:
        # Simulate a logged-in browser: SessionMiddleware wraps this middleware
        # (see src/main.py's create_app — badge middleware is added first, so
        # it runs INSIDE session middleware) and populates scope["session"] on
        # every request, including asset requests, since browsers send cookies
        # for same-origin static assets too.
        scope["session"] = {"user_id": user_id}
    return Request(scope)


@pytest.mark.asyncio
async def test_static_and_health_requests_never_touch_the_db(monkeypatch):
    import src.main as main_mod

    # A raising sentinel doesn't work here: AgentBadgeMiddleware.dispatch wraps
    # the whole DB path in a broad `except Exception` (deliberately, so a badge
    # count failure never 500s a page), which would swallow an AssertionError
    # raised from inside get_session_factory() and let the test pass either
    # way. Record invocation instead, and assert on that directly.
    calls: list[None] = []

    def _record_call():
        calls.append(None)
        raise AssertionError("session factory must not be used on this path")

    monkeypatch.setattr(main_mod, "get_session_factory", _record_call)

    async def call_next(request):
        return "downstream"

    mw = AgentBadgeMiddleware(app=None)
    uid = "11111111-1111-1111-1111-111111111111"
    for path in ("/static/app.css", "/api/health"):
        assert await mw.dispatch(_request(path, user_id=uid), call_next) == "downstream"
    assert calls == [], "badge middleware queried the DB for a static/health request"
