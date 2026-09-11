"""The FastAPI app's lifespan shuts the Slack I/O executor down on exit.

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
from src.services.slack_executor import run_slack_call


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


@pytest.mark.asyncio
async def test_a_real_lifespan_shutdown_does_not_permanently_kill_run_slack_call():
    """The REAL (unmocked) shutdown_slack_executor() must not leave the
    module-level pool permanently shut down. Any pytest process that drives
    this app's real ASGI lifespan even once (many integration tests do, for
    reasons that have nothing to do with Slack) would otherwise break
    run_slack_call for every OTHER test sharing that interpreter, regardless
    of whether that test ever touched shutdown itself. The pool must be
    lazily re-created on the next call instead.

    This drives the REAL
    ``shutdown_slack_executor()``, which also sets
    ``slack_client.SHUTTING_DOWN`` (the module-level fallback) as a side
    effect — leaving that Event set for the rest of this pytest process
    would abort every OTHER test's retry sleep bound to it (any
    ``AgentSlackClient`` call made on a thread never bound to a pool, e.g.
    ``tests/unit/test_slack_client_contract.py``'s own tests). Clear it back
    afterward, the same way ``test_slack_executor.py``'s autouse fixture
    does for that file's tests.
    """
    from src.agent.slack_client import SHUTTING_DOWN

    app = create_app()
    try:
        async with app.router.lifespan_context(app):
            pass  # real shutdown_slack_executor() runs here, unmocked

        result = await run_slack_call(lambda: 42)

        assert result == 42
    finally:
        SHUTTING_DOWN.clear()
