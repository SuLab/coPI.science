"""The FastAPI app's lifespan shuts the Slack I/O executor down on exit (opus
review follow-up to R-2, audit 2026-09-10).

A bare module-level ``ThreadPoolExecutor`` (src/services/slack_executor.py) is
never torn down on its own — an in-flight Slack call blocked on a sustained
throttle (up to 180s) could hold interpreter shutdown open past `docker stop
-t 30`'s grace period, which then SIGKILLs the process. `create_app()` wires
`lifespan` into `FastAPI(...)`; this test drives `lifespan` directly rather
than spinning the whole ASGI app (which needs DB settings this test has no
reason to depend on).
"""

import pytest

import src.main as main_module
from src.main import create_app, lifespan


@pytest.mark.asyncio
async def test_lifespan_shuts_down_the_slack_executor_on_exit(monkeypatch):
    calls = []
    monkeypatch.setattr(main_module, "shutdown_slack_executor", lambda: calls.append(True))

    async with lifespan(app=None):
        assert calls == [], "must not shut the executor down on startup"

    assert calls == [True], "must shut the executor down on shutdown"


@pytest.mark.asyncio
async def test_create_app_actually_drives_the_lifespan_shutdown(monkeypatch):
    """FastAPI wraps a passed ``lifespan=`` in its own merged context manager (it
    does not store the callable we passed verbatim), so this drives the router's
    actual lifespan context rather than comparing identity to ``lifespan``."""
    calls: list = []
    monkeypatch.setattr(main_module, "shutdown_slack_executor", lambda: calls.append(True))
    app = create_app()
    async with app.router.lifespan_context(app):
        assert calls == []
    assert calls == [True]
