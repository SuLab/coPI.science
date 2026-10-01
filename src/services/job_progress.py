"""Job progress entries (`jobs.payload['progress']`), written in their OWN short
transaction (spec §9.2 DP-09).

A handler's pipeline transaction never modifies its jobs row: that row used to be
dirtied by every progress append and so stayed row-locked for the whole run, and
progress only became visible at the final commit. `record` commits each entry on
its own session, so /onboarding shows progress mid-run, and a failed run keeps the
progress that explains it. Best-effort: a failed write never fails the job (and
logs nothing, so the pipeline's log stream is unchanged).
"""
from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] | None = None

_APPEND_SQL = text(
    "UPDATE jobs SET payload = jsonb_set("
    "  COALESCE(payload::jsonb, '{}'::jsonb), '{progress}',"
    "  COALESCE(payload::jsonb -> 'progress', '[]'::jsonb)"
    "  || jsonb_build_array(jsonb_build_object("
    "       'step', CAST(:step AS text), 'detail', CAST(:detail AS text)))"
    ")::json WHERE id = :id"
)


def configure(factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] | None) -> None:
    """Point `record` at a session factory (the worker's; a test's). None restores
    the default, `src.database.get_session_factory()`."""
    global _factory
    _factory = factory


async def record(job_id: uuid.UUID | None, step: str, detail: str = "") -> None:
    """Append `{"step", "detail"}` to the job's progress and commit it at once."""
    if job_id is None:
        return
    factory = _factory
    if factory is None:
        from src.database import get_session_factory
        factory = get_session_factory()
    with contextlib.suppress(Exception):
        async with factory() as session:
            await session.execute(_APPEND_SQL, {"id": job_id, "step": step, "detail": detail})
            await session.commit()
