"""Daily persona sweep (spec 2026-10-05 §6.1 "Daily sweep", D54).

Grant sections depend on the date (an award ends; an ORCID funding passes its five-year
window), so a persona can go stale with no write to any table. Once a day, from the worker's
idle loop, every agent with a user, a profile and a pi_grant_identity row is rendered afresh
and re-exported through profile_publish (mechanism ``persona_sweep``) when the file differs.
PIs without an identity row are skipped. Gated by the app setting ``persona_sweep_enabled``,
which the grants repair (scripts/grants_remediation.py) sets as its last step. No job type:
it is idle work, like the chat-suggestion sweep."""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentRegistry, AppSetting, PiGrantIdentity, ResearcherProfile
from src.services.profile_publish import reexport_persona, write_persona_files

logger = logging.getLogger(__name__)

PERSONA_SWEEP_SETTING = "persona_sweep_enabled"
#: The sweep runs at most once per this many seconds.
PERSONA_SWEEP_SECONDS = 24 * 3600
#: While the flag is off, the worker re-checks it this often.
PERSONA_SWEEP_RECHECK_SECONDS = 300


async def sweep_enabled(db: AsyncSession) -> bool:
    """True when the ``persona_sweep_enabled`` setting is ``"true"`` (case-insensitive)."""
    value = await db.scalar(
        select(AppSetting.value).where(AppSetting.key == PERSONA_SWEEP_SETTING)
    )
    return (value or "").strip().lower() == "true"


async def enable_persona_sweep(db: AsyncSession) -> None:
    """Set the flag (the repair's last step). Adds to the caller's transaction."""
    await db.execute(
        pg_insert(AppSetting)
        .values(key=PERSONA_SWEEP_SETTING, value="true")
        .on_conflict_do_update(
            index_elements=["key"], set_={"value": "true", "updated_at": func.now()}
        )
    )


async def _candidates(db: AsyncSession) -> list[uuid.UUID]:
    """User ids of every agent with a user, a profile and a pi_grant_identity row."""
    return list((await db.execute(
        select(AgentRegistry.user_id)
        .join(PiGrantIdentity, PiGrantIdentity.user_id == AgentRegistry.user_id)
        .join(ResearcherProfile, ResearcherProfile.user_id == AgentRegistry.user_id)
        .where(AgentRegistry.user_id.isnot(None))
        .order_by(AgentRegistry.agent_id)
    )).scalars().all())


async def run_persona_sweep(session_factory) -> int | None:
    """Re-export every persona whose file differs from a fresh render, one session per PI;
    returns how many were re-exported, or None while the flag is off. A failure for one PI
    is logged and the sweep moves on."""
    async with session_factory() as db:
        if not await sweep_enabled(db):
            return None
        user_ids = await _candidates(db)
    exported = 0
    for user_id in user_ids:
        async with session_factory() as db:
            try:
                text = await reexport_persona(
                    db, user_id, mechanism="persona_sweep", skip_if_file_matches=True,
                    change_summary="Daily sweep: the persona file differed from a fresh render",
                )
                if text is None:
                    await db.rollback()
                    continue
                await db.commit()
            except Exception:
                logger.exception("Persona sweep: re-export failed for user %s", user_id)
                await db.rollback()
                continue
            await write_persona_files(db, user_id)
            exported += 1
    if exported:
        logger.info("Persona sweep re-exported %d persona(s)", exported)
    return exported
