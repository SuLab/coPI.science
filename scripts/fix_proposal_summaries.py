# scripts/fix_proposal_summaries.py
import asyncio, re
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from src.config import get_settings
from src.models import ThreadDecision

async def fix():
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as db:
        result = await db.execute(
            select(ThreadDecision).where(ThreadDecision.outcome == "proposal")
        )
        decisions = result.scalars().all()
        fixed = 0
        for d in decisions:
            if not d.summary_text:
                continue
            match = re.search(r"<proposal>(.*?)</proposal>", d.summary_text, re.DOTALL)
            if match:
                d.summary_text = match.group(1).strip()
                fixed += 1
        await db.commit()
        print(f"Fixed {fixed} / {len(decisions)} proposals")

asyncio.run(fix())
