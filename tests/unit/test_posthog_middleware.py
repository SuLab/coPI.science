"""The kept half of the retired nav-badge middleware."""

import types

import pytest
from starlette.requests import Request

import src.main as main_mod
from src.main import PostHogContextMiddleware


def _request(path: str) -> Request:
    scope = {
        "type": "http", "method": "GET", "path": path, "headers": [],
        "query_string": b"", "session": {"user_id": "11111111-1111-1111-1111-111111111111"},
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_posthog_middleware_skips_static_and_health(monkeypatch):
    calls: list[None] = []

    def _settings():
        calls.append(None)
        return types.SimpleNamespace(posthog_api_key="phc_x")

    monkeypatch.setattr(main_mod, "get_settings", _settings)

    async def call_next(request):
        return request

    mw = PostHogContextMiddleware(app=None)
    for path in ("/static/app.css", "/api/health"):
        req = await mw.dispatch(_request(path), call_next)
        assert not hasattr(req.state, "posthog_api_key"), path
    assert calls == [], "settings were read for a static/health request"


@pytest.mark.asyncio
async def test_posthog_middleware_sets_the_key_on_a_page_request(monkeypatch):
    monkeypatch.setattr(
        main_mod, "get_settings", lambda: types.SimpleNamespace(posthog_api_key="phc_x")
    )

    async def call_next(request):
        return request

    req = await PostHogContextMiddleware(app=None).dispatch(_request("/login"), call_next)
    assert req.state.posthog_api_key == "phc_x"


def test_the_badge_middleware_is_gone():
    assert not hasattr(main_mod, "AgentBadgeMiddleware")
    names = [m.cls.__name__ for m in main_mod.create_app().user_middleware]
    assert "PostHogContextMiddleware" in names
    assert names[0] == "OriginGuardMiddleware"
