"""Record the rubric document an engine loaded (AP-7). WRITE-ONLY in this program
(FA2-V4): provenance keeps resolving from the live rubric, then revisions.toml,
then unknown (rubric_revisions). A DB copy, never a file change (B22)."""

from __future__ import annotations

import hashlib

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import RubricDocument
from src.services.blackbird_rubric import RUBRIC_CONTENT_HASH, RUBRIC_VERSION, loaded_rubric_bytes


async def record_loaded_rubric(db: AsyncSession) -> None:
    """Insert the loaded rubric keyed by content hash; an existing row is kept."""
    raw = loaded_rubric_bytes()
    await db.execute(
        pg_insert(RubricDocument)
        .values(content_hash=RUBRIC_CONTENT_HASH, sha256=hashlib.sha256(raw).hexdigest(),
                version=RUBRIC_VERSION, toml=raw.decode("utf-8"))
        .on_conflict_do_nothing(index_elements=["content_hash"])
    )
    await db.commit()
