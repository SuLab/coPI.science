"""start_provisioning awaits synchronous httpx calls (and, on a Slack 429,
time.sleep(retry_after) loops) — on the single-worker web loop that is a
site-wide freeze (issue #24 C2; nginx's 120s proxy_read_timeout turns it
into a 504). The stub below stands in for the blocking manifest call; the
heartbeat asserts the loop stays live while it runs."""
import asyncio
import time

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.models import AgentRegistry

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_start_provisioning_does_not_block_the_loop(engine, monkeypatch):
    import src.services.admin_provisioning as ap

    def slow_create_app(**kwargs):
        time.sleep(0.5)  # what the real sync httpx.post + retry sleep does
        return {
            "agent_id": kwargs["agent_id"], "bot_name": kwargs["bot_name"],
            "pi_name": kwargs["pi_name"], "app_id": "A1",
            "client_id": "c", "client_secret": "s",
            "oauth_url": "https://slack.test/oauth?x=1",
        }

    monkeypatch.setattr(ap, "create_app", slow_create_app)

    async def fake_config_token(db, *, force_rotate=False):
        return "xoxe.xoxp-config"
    monkeypatch.setattr(ap, "_config_token", fake_config_token)
    monkeypatch.setattr(ap, "lookup_team_id", lambda token: None)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        agent = AgentRegistry(
            agent_id="stub", bot_name="StubBot", pi_name="Stub PI",
            status="pending",
        )
        db.add(agent)
        await db.commit()

        gaps: list[float] = []
        stop = asyncio.Event()

        async def heartbeat():
            last = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.05)
                now = time.monotonic()
                if now - last > 0.25:
                    gaps.append(now - last)
                last = now

        hb = asyncio.create_task(heartbeat())
        # Let the heartbeat task actually start (record its first `last`
        # timestamp and park on asyncio.sleep(0.05)) before calling
        # start_provisioning. asyncio.create_task only SCHEDULES the task;
        # it doesn't run until the current coroutine yields to the loop. The
        # faked _config_token below has no internal await that suspends, so
        # without this yield the (still-synchronous, pre-fix) blocking call
        # would run and finish before the heartbeat ever executes its first
        # line — producing a false pass that hides the freeze entirely.
        # Verified empirically both ways in scratch repros before adding
        # this line.
        await asyncio.sleep(0)
        url = await ap.start_provisioning(db, agent)
        stop.set()
        await hb
        assert url.startswith("https://slack.test/oauth")
        assert not gaps, f"event loop froze during provisioning: {gaps}"
