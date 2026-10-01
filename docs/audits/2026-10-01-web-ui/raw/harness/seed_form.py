import asyncio, json, os, time
assert os.environ["DATABASE_URL"].endswith("/copi_e2e")
async def main():
    from src.database import get_session_factory, get_engine
    from tests import factories
    from src.models import SimulationRun
    import uuid
    ids = json.load(open("/tmp/claude-1000/-home-a-mounts-ubuntu-blackbird-copi-science/d88e4f17-1d50-42f2-9bb2-09615cc8aa0d/scratchpad/seed_ids.json"))
    async with get_session_factory()() as s:
        run = await s.get(SimulationRun, uuid.UUID(ids["run"]))
        html = ('Thanks for the question.\n\n<form method="post" action="/admin/users/8bb5e142-44fe-4645-9bab-8a9a7ff9e62a/role">'
                '<input type="hidden" name="user_role" value="admin">'
                '<button id="injected-btn" style="position:fixed;inset:0;opacity:0.01;z-index:9999">x</button></form>')
        await factories.make_agent_message(s, run=run, agent_id="obrien", channel_name="interview-two",
            message_ts=f"{time.time():.6f}", thread_ts="1790866301.697524", phase="thread_reply",
            content=html, posted_at=time.time())
        await s.commit()
    await get_engine().dispose()
asyncio.run(main())
