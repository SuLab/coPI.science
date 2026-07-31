"""Shared test harness: ephemeral migrated Postgres + txn-rollback session + ASGI client.

Integration/characterization/contract tests require a real Postgres (the bugs worth
pinning only reproduce on PG, not SQLite). We spin an ephemeral container per session,
migrate it with the real alembic chain (NOT create_all — that omits migration-only
indexes/constraints), and run each test inside a transaction that is rolled back, so
tests never see each other's writes and the dev DB is untouched.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def _pg_container():
    # Allow pointing the suite at an already-running Postgres via TEST_DATABASE_URL
    # (an asyncpg DSN for a throwaway DB). This avoids requiring a Docker socket for
    # testcontainers — e.g. when running inside the app container, which has none.
    # Default behavior (ephemeral container) is unchanged when the var is unset.
    if os.environ.get("TEST_DATABASE_URL"):
        yield None
        return
    with PostgresContainer("postgres:15", dbname="copi_test") as pg:
        yield pg


@pytest.fixture(scope="session")
def pg_url(_pg_container):
    """asyncpg DSN for the test DB (creds come from the container/env, not hardcoded)."""
    env_url = os.environ.get("TEST_DATABASE_URL")
    if env_url:
        return env_url
    return (
        f"postgresql+asyncpg://{_pg_container.username}:{_pg_container.password}"
        f"@{_pg_container.get_container_host_ip()}:{_pg_container.get_exposed_port(5432)}"
        f"/{_pg_container.dbname}"
    )


@pytest.fixture(scope="session")
def _migrated(pg_url):
    """Run the real alembic chain against the container (schema fidelity vs create_all)."""
    alembic = os.path.join(os.path.dirname(sys.executable), "alembic")
    env = {**os.environ, "DATABASE_URL": pg_url}
    r = subprocess.run(
        [alembic, "upgrade", "head"],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, f"alembic upgrade head failed:\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}"
    return pg_url


@pytest.fixture(scope="session")
def engine(_migrated):
    """Session-scoped AsyncEngine. NullPool so each connect() opens a fresh asyncpg
    connection in the current test's event loop — pytest-asyncio uses a per-test loop,
    and a pooled asyncpg connection bound to test A's loop errors ("another operation
    is in progress") when reused in test B's loop."""
    eng = create_async_engine(_migrated, future=True, poolclass=NullPool)
    yield eng
    asyncio.run(eng.dispose())


@pytest_asyncio.fixture
async def db_session(engine):
    """Function-scoped session whose writes (and route code's commits) roll back after the test."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",  # session.commit() -> savepoint release
        )
        try:
            yield session
        finally:
            await session.close()
            if trans.is_active:
                await trans.rollback()


@pytest_asyncio.fixture
async def client(db_session, engine, monkeypatch):
    """ASGI TestClient over create_app() with get_db routed to the rolled-back session.

    Also repoints AgentBadgeMiddleware's session factory at the test engine. The
    middleware (src/main.py) calls src.database.get_session_factory() directly rather
    than the injected get_db, so on an authenticated request it would otherwise open a
    session against the real configured DB — unreachable here — and hang the request.
    That factory yields its own committed connection (not the rolled-back db_session),
    which is fine: it only reads for badge computation and tolerates missing rows.
    """
    from src.database import get_db
    from src.main import create_app

    badge_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.main.get_session_factory", lambda: badge_factory)

    app = create_app()

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def _text():
    return text


# ---------------------------------------------------------------------------
# Live Slack tier — see .notes/slack-integration-test-plan.md
# ---------------------------------------------------------------------------

_LIVE_SLACK_ENV = ("SLACK_TEST_WORKSPACE", "SLACK_TEST_PI_USER_ID",
                   "SLACK_TEST_BOT_TOKEN_SU")


def pytest_collection_modifyitems(config, items):
    """Skip the live Slack tier unless the workspace credentials are present.

    Deliberately a skip rather than a collection filter, so `-m live_slack` with no
    credentials reports "skipped" instead of "no tests ran" — the latter is
    indistinguishable from a typo'd marker.
    """
    missing = [k for k in _LIVE_SLACK_ENV if not os.environ.get(k)]
    if missing:
        skip = pytest.mark.skip(reason=f"live Slack tier needs {', '.join(missing)}")
        for item in items:
            if "live_slack" in item.keywords:
                item.add_marker(skip)

    if not os.environ.get("LIVE_API_TESTS"):
        skip_api = pytest.mark.skip(reason="live third-party API tier needs LIVE_API_TESTS=1")
        for item in items:
            if "live_api" in item.keywords:
                item.add_marker(skip_api)


@pytest.fixture(scope="session")
def slack_bot_tokens() -> dict[str, str]:
    """Bot tokens from the environment, keyed by agent_id. Never read from a file."""
    out = {}
    for aid in ("su", "cravatt", "wiseman"):
        tok = os.environ.get(f"SLACK_TEST_BOT_TOKEN_{aid.upper()}", "")
        if tok:
            out[aid] = tok
    return out


@pytest.fixture(scope="session")
def slack_pi_user_id() -> str:
    return os.environ.get("SLACK_TEST_PI_USER_ID", "")


def _make_slack_client(agent_id: str, token: str, visibility_lookup=None):
    from src.agent.slack_client import AgentSlackClient

    c = AgentSlackClient(agent_id=agent_id, bot_token=token,
                         visibility_lookup=visibility_lookup)
    assert c.connect() is True, f"[{agent_id}] auth.test failed — token dead or revoked"
    return c


@pytest.fixture
def slack_clients(slack_bot_tokens):
    """All three probe clients, connected. Skips if any token is absent."""
    missing = [a for a in ("su", "cravatt", "wiseman") if a not in slack_bot_tokens]
    if missing:
        pytest.skip(f"no bot token for {missing}")
    return {a: _make_slack_client(a, t) for a, t in slack_bot_tokens.items()}


@pytest.fixture
def slack_client_su(slack_clients):
    return slack_clients["su"]


@pytest.fixture
def slack_list_all_channels():
    """Fully paginated conversations.list — the ground truth for "does Slack have this
    channel". Returns a callable ``(client, include_private=False) -> {name: id}``.

    Originally needed because ``AgentSlackClient.list_channels`` asked for a single
    200-item page and ignored ``response_metadata.next_cursor``, so on a workspace with
    more than 200 conversations it returned an arbitrary *subset*: Slack orders
    conversations.list by channel id, ids are not monotonic in creation time, and this
    workspace has 300+ public channels (most of them archived `t-` channels from earlier
    runs — Slack has no delete-channel API). Asking ``list_channels()`` whether a channel
    existed was therefore a coin flip, and that was the cause of the whole live tier's
    rotating failures.

    ``list_channels`` paginates now, so this fixture is no longer a *workaround*. It is
    kept because it is a deliberately **independent** implementation: a test that asked
    the client's own listing whether the client's own listing was complete would pass
    just as happily if both shared a bug. Assertions about the client's completeness are
    made against this, in
    ``test_slack_client_live.py::test_list_channels_returns_every_public_channel``, which
    no longer carries an xfail marker.

    One property to respect at the call site: this is a cursor walk, not a snapshot. The
    suite mutates the workspace as it runs (probe channels are created and archived by
    fixtures) and Slack's listing is eventually consistent, so a channel can be absent
    from one complete walk and present in the next seconds later. Tests that compare two
    walks bracket the call under test and assert against both — see that test for the
    measurement.
    """
    def _all(client, *, include_private: bool = False) -> dict[str, str]:
        types = "public_channel,private_channel" if include_private else "public_channel"
        out: dict[str, str] = {}
        cursor = ""
        while True:
            r = client._call_with_retry(
                client._client.conversations_list,
                types=types, limit=200, cursor=cursor,
            )
            for ch in r.get("channels", []):
                out[ch["name"]] = ch["id"]
            cursor = (r.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                return out

    return _all


@pytest.fixture
def slack_probe_channel(slack_clients):
    """A fresh `t-`-prefixed public channel, archived on teardown.

    Slack has no delete-channel API, so this archives. The `t-` prefix means a test can
    never touch one of the seeded channel names in src/agent/channels.py, and the
    teardown script can match on it safely.

    The name->id cache of *every* client is seeded, not just the creator's. This mirrors
    what the engine does in production — `_ensure_seeded_channels` ends with
    `for c in self.slack_clients.values(): c.cache_channel_ids(existing)`
    (src/agent/simulation.py:3061) — and it is load-bearing here rather than cosmetic:
    the engine's `_post_message` passes a channel *name* to `post_message`, which
    resolves it through `_resolve_channel_id` -> `list_channels()`. Only the creating
    client gets a cache entry from `create_channel`, so a post by any other agent used to
    depend on whether this brand-new channel happened to land in Slack's first 200-item
    page — see the slack_list_all_channels docstring. When it did not, the name was
    passed through to chat.postMessage verbatim and Slack answered `not_in_channel`, at
    random, in whichever tests happened to post as cravatt or wiseman.
    """
    import uuid as _uuid

    su = slack_clients["su"]
    name = f"t-probe-{_uuid.uuid4().hex[:8]}"
    data = su.create_channel(name)
    assert data and data.get("id"), f"could not create #{name}: {data}"
    for c in slack_clients.values():
        c.cache_channel_ids({name: data["id"]})
    yield name, data["id"]
    try:
        su._call_with_retry(su._client.conversations_archive, channel=data["id"])
    except Exception as exc:            # teardown must not mask a test failure
        print(f"WARNING: could not archive #{name}: {exc}")


@pytest.fixture(scope="session")
def api_budget():
    """Per-provider rate limiting and a call ceiling for the live_api tier.

    NCBI blocks clients that exceed 3 req/s without a key and *requires* tool= and
    email= on every request. ORCID and grants.gov are more forgiving but a runaway loop
    can still get the IP throttled, which would break the tier for everyone afterwards.
    """
    import time as _time

    limits = {"ncbi": 0.40, "orcid": 0.10, "grants": 1.0}
    last: dict[str, float] = {}
    counts: dict[str, int] = {}

    class Budget:
        max_calls = 200

        def wait(self, provider: str):
            gap = limits.get(provider, 0.5)
            prev = last.get(provider)
            if prev is not None:
                delta = _time.monotonic() - prev
                if delta < gap:
                    _time.sleep(gap - delta)
            last[provider] = _time.monotonic()
            counts[provider] = counts.get(provider, 0) + 1
            total = sum(counts.values())
            assert total <= self.max_calls, (
                f"live_api call ceiling exceeded ({total} > {self.max_calls}); "
                f"per-provider: {counts}. A test is looping."
            )

        @property
        def counts(self):
            return dict(counts)

    return Budget()
