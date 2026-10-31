"""Whether migration 0077 has run, so landscaping code can refuse cleanly before it has.

The landscaping worker and the landscape pages call ``schema_ready`` first and refuse when it
is false (spec §12.5): new code on the old schema must not reach a ``landscape_`` table.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def schema_ready(db: AsyncSession) -> bool:
    """True when ``landscape_epochs`` exists: 0077 creates it, and its downgrade drops it."""
    found = await db.scalar(text("SELECT to_regclass('public.landscape_epochs') IS NOT NULL"))
    return bool(found)
