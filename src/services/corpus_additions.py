"""Which resolved records a PI with a stored corpus gets (SC-5), shared by the
profile pipeline and scripts/repair_pi_corpus.py. Extracted verbatim from the
pipeline: never delete; add only ORCID-anchored finds (stages s1/s3); s2/s4-only
candidates are for review; respect the cap (audit M4). Also the corpus advisory
lock both writers take around publication writes (SC-6)."""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.services.advisory_locks import entity_key_sql

_ANCHOR_STAGES = {"s1", "s3"}


@dataclass(frozen=True)
class CorpusAdditions:
    to_store: list[dict[str, Any]]
    over_cap: list[dict[str, Any]]
    review_only: list[dict[str, Any]]


def select_corpus_additions(
    kept: Sequence[dict[str, Any]],
    stored_pmids: Collection[str],
    stored_count: int,
    cap: int,
) -> CorpusAdditions:
    """Split the resolver's kept set into what to store, what the cap has no
    room for, and the unanchored candidates held for review."""
    new_recs = [r for r in kept if r.get("pmid") and r["pmid"] not in stored_pmids]
    anchored = [r for r in new_recs if set(r.get("stages") or []) & _ANCHOR_STAGES]
    review_only = [r for r in new_recs if not (set(r.get("stages") or []) & _ANCHOR_STAGES)]
    budget = max(0, cap - stored_count)
    return CorpusAdditions(anchored[:budget], anchored[budget:], review_only)


async def lock_corpus(db: AsyncSession, user_id: uuid.UUID) -> None:
    """pg_advisory_xact_lock on corpus:<user_id>; held until the caller's commit."""
    await db.execute(
        text(f"SELECT pg_advisory_xact_lock({entity_key_sql('corpus')})"),
        {"id": str(user_id)},
    )
