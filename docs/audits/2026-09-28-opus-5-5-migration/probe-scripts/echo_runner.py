import asyncio, json, importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("opus55_replay", "/app/scripts/dev/opus55_replay.py")
R = importlib.util.module_from_spec(spec); sys.modules["opus55_replay"] = R; spec.loader.exec_module(R)
from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from src.config import get_settings
async def main():
    s = get_settings()
    eng = create_async_engine(s.database_url, poolclass=NullPool, execution_options={"postgresql_readonly": True})
    budget = R.Budget(Decimal("3"), 4)
    try:
        async with AsyncSession(eng, expire_on_commit=False) as db:
            out = await R.echo_probe(R._client(), budget, db)
    finally:
        await eng.dispose()
    print(json.dumps({"echo_probe": out, "spent_usd": float(budget.spent), "calls": budget.calls}, default=str))
asyncio.run(main())
