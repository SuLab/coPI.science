"""Whether a simulation run is live, the signal synchronous landscaping work yields to (D33).

"Live" means the engine advisory lock is held (``simulation_control.engine_alive``), the
signal the simulation panel reads. A stale heartbeat with the lock held is still a live run
(spec §12.2). Wrapped here so stage runners and their tests have one seam to fake.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from src.services import simulation_control


async def simulation_live(db: AsyncSession) -> bool:
    """True while some process in this database holds the engine lock."""
    return await simulation_control.engine_alive(db)
