"""Derive an agent's (agent_id, bot_name) pair from a PI's display name.

Moved out of ``src/routers/agent_page.py`` so the manager Add-PI flow can mint
agents through the same logic as self-service signup. Two divergent copies
still live in scripts (``scripts/backfill_agents.py`` rebuilds bot names with
``.capitalize()`` → ``MccarthyBot``; ``scripts/generate_sparsedata_user.py``
has its own tiers) — this module is the web-facing truth: display casing is
preserved (``McCarthyBot``).
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentRegistry

logger = logging.getLogger(__name__)

# Numeric tier bound, matching scripts/backfill_agents.py's range(2, 20).
_NUMERIC_TIER_MAX = 20


async def _is_taken(db: AsyncSession, agent_id: str) -> bool:
    row = await db.execute(
        select(AgentRegistry.id).where(AgentRegistry.agent_id == agent_id)
    )
    return row.first() is not None


async def derive_agent_identity(
    db: AsyncSession, full_name: str, orcid: str | None = None
) -> tuple[str, str]:
    """Return ``(agent_id, bot_name)`` for a PI's display name.

    Both values are derived here, together, because they must agree: the
    collision prefix used to be applied to agent_id at one line and bot_name
    rebuilt from the bare last name four lines later, so Peng Wu got
    ``pwu`` / ``WuBot`` — colliding with Chunlei Wu's bot while the ids
    differed. CLAUDE.md documents ``pwu`` / ``PWuBot``.

    Tiers: bare last-name stem → first-initial prefix → numeric suffix
    (``wu2``..``wu19``). The numeric tier exists because the manager Add-PI
    flow creates rows without a human in the loop, and a second collision
    used to surface as an IntegrityError on the unique ``agents.agent_id``.

    A display name with no alphabetic characters (seen for ORCID records
    whose name never resolved) would otherwise yield an EMPTY agent_id
    silently; ``orcid`` provides the fallback stem (``pi6789``).
    """
    last_name = full_name.split()[-1] if full_name.split() else ""
    stem = "".join(c for c in last_name.lower() if c.isalpha())
    display = last_name

    if not stem:
        digits = "".join(c for c in (orcid or "") if c.isdigit())
        stem = f"pi{digits[-4:]}" if digits else "lab"
        display = stem.capitalize()
        logger.warning(
            "derive_agent_identity: no alphabetic last name in %r; "
            "falling back to stem %r", full_name, stem,
        )

    if not await _is_taken(db, stem):
        return stem, f"{display}Bot"

    initial = full_name[0] if full_name else stem[0]
    if initial.isalpha():
        prefixed = f"{initial.lower()}{stem}"
        if not await _is_taken(db, prefixed):
            return prefixed, f"{initial.upper()}{display}Bot"

    for n in range(2, _NUMERIC_TIER_MAX):
        candidate = f"{stem}{n}"
        if not await _is_taken(db, candidate):
            return candidate, f"{display}{n}Bot"

    raise RuntimeError(
        f"Could not derive a free agent_id for {full_name!r} after "
        f"{_NUMERIC_TIER_MAX - 2} numeric candidates"
    )
