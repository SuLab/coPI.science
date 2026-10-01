import asyncio, json, os
assert os.environ["DATABASE_URL"].endswith("/copi_e2e")
async def main():
    from src.database import get_session_factory, get_engine
    from tests import factories
    async with get_session_factory()() as s:
        u1 = await factories.make_user(s, name="x'+(window.__xss=1)+'", orcid="0000-0002-1111-0091", email="x1@uiaudit.test")
        u2 = await factories.make_user(s, name="Mary O'Brien", orcid="0000-0002-1111-0092", email="x2@uiaudit.test")
        await s.commit()
        print(json.dumps({"xss_user": str(u1.id), "apos_user": str(u2.id)}))
    await get_engine().dispose()
asyncio.run(main())
