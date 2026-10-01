import asyncio, json, os, time
assert os.environ["DATABASE_URL"].endswith("/copi_e2e")
async def main():
    from src.database import get_session_factory, get_engine
    from tests import factories
    from sqlalchemy import select
    from src.models import SimulationRun
    ids = json.load(open(os.environ["IDS"]))
    async with get_session_factory()() as s:
        run = await s.get(SimulationRun, __import__("uuid").UUID(ids["run"]))
        long = ("Paragraph of a very long hub reply. " * 40 + "\n\n") * 6
        await factories.make_agent_message(s, run=run, agent_id="blackbird", channel_name="interview-two",
            message_ts=f"{time.time():.6f}", thread_ts="1790866301.697524", phase="thread_reply",
            content=long, posted_at=time.time())
        await s.commit()
    await get_engine().dispose()
asyncio.run(main())
