"""One-time migration: the curated agent_id-keyed JHU tenure map → per-user rows.

The 2026-08-13 deployment stored ``jhu_tenure_start`` as a single app_settings
row keyed by agent_id. Pipeline runs for users WITHOUT an agent row (CLI
seeding, self-signup before /agent/request) have no agent_id to look up, so
new entries are keyed by user_id (``jhu_tenure_start:{user_id}``) — see
src/services/jhu_rules.py. This script rewrites the curated entries into that
form with ``source="curated-2026-08-13"``. The legacy row is left in place
(read as a fallback; historical record).

Run inside the app container:
    docker compose -f docker-compose.prod.yml exec blackbird-app \
        python scripts/migrate_tenure_map.py

Idempotent: an existing per-user entry is never clobbered (a manual
correction outranks the curated import); unresolvable agent_ids are reported,
not guessed at.
"""

import asyncio
import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import get_session_factory
from src.models import AgentRegistry, AppSetting
from src.services.jhu_rules import (
    LEGACY_TENURE_KEY,
    TENURE_KEY_PREFIX,
    set_tenure_start,
)

logger = logging.getLogger(__name__)


async def migrate_tenure_map(db: AsyncSession) -> dict:
    """Migrate legacy entries; returns {"migrated", "skipped", "unresolved"}."""
    row = await db.execute(
        select(AppSetting.value).where(AppSetting.key == LEGACY_TENURE_KEY)
    )
    raw = row.scalar_one_or_none()
    if not raw:
        return {"migrated": 0, "skipped": 0, "unresolved": []}

    legacy: dict[str, int] = json.loads(raw)
    migrated = 0
    skipped = 0
    unresolved: list[str] = []

    for agent_id, year in sorted(legacy.items()):
        agent = (
            await db.execute(
                select(AgentRegistry).where(AgentRegistry.agent_id == agent_id)
            )
        ).scalar_one_or_none()
        if agent is None or agent.user_id is None:
            unresolved.append(agent_id)
            continue
        existing = await db.get(AppSetting, f"{TENURE_KEY_PREFIX}{agent.user_id}")
        if existing is not None:
            skipped += 1
            continue
        await set_tenure_start(
            agent.user_id, int(year), "curated-2026-08-13", db=db
        )
        migrated += 1

    return {"migrated": migrated, "skipped": skipped, "unresolved": unresolved}


async def _main() -> None:
    factory = get_session_factory()
    async with factory() as db:
        report = await migrate_tenure_map(db)
        await db.commit()
    print(
        f"migrated={report['migrated']} skipped={report['skipped']} "
        f"unresolved={report['unresolved']}"
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
