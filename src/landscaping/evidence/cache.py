"""The evidence cache, ``landscape_evidence_cache`` (spec §7.4 step 5, §11.2).

A lookup is stored once per (source, release, query key). The release is the source's
published release where it has one (Open Targets, ChEMBL, Mondo, HGNC), else the ISO week of
the fetch, so a release-less source is re-fetched weekly. Storing a row under a newer release
deletes that source's rows from older releases.
"""
from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.landscaping.evidence import chembl, hgnc, mondo, opentargets
from src.models.landscape import LandscapeEvidenceCache

#: The sources whose release is the source's own published one; every other source's
#: release is `iso_week`.
RELEASE_RESOLVERS: dict[str, Callable[[], Awaitable[str]]] = {
    "opentargets": opentargets.ot_release,
    "chembl": chembl.chembl_release,
    "mondo": mondo.mondo_release,
    "hgnc": hgnc.hgnc_release,
}
#: Seconds a resolved release is reused before the source is asked again.
RELEASE_TTL_S = 6 * 3600
#: ``query_key``'s column width; a longer key is shortened with a hash suffix.
KEY_MAX = 500

_release_memo: dict[str, tuple[float, str]] = {}
_TOKEN = re.compile(r"\d+|\D+")


def iso_week(today: date | None = None) -> str:
    """``2026-W44``: the release of a source that publishes none."""
    year, week, _ = (today or datetime.now(UTC).date()).isocalendar()
    return f"{year}-W{week:02d}"


async def current_release(source: str) -> str:
    """The release to cache ``source``'s lookups under now. A published release is asked of
    the source at most every `RELEASE_TTL_S`."""
    resolver = RELEASE_RESOLVERS.get(source)
    if resolver is None:
        return iso_week()
    memo = _release_memo.get(source)
    now = time.monotonic()
    if memo is not None and now - memo[0] < RELEASE_TTL_S:
        return memo[1]
    release = await resolver()
    _release_memo[source] = (now, release)
    return release


def _release_order(release: str) -> tuple:
    """Natural sort key: ``ChEMBL_37`` < ``ChEMBL_100``, ``26.06`` < ``26.09``."""
    return tuple((0, int(t), "") if t.isdigit() else (1, 0, t)
                 for t in _TOKEN.findall(release))


def _query_key(key: str) -> str:
    if len(key) <= KEY_MAX:
        return key
    digest = hashlib.sha256(key.encode()).hexdigest()
    return f"{key[:KEY_MAX - len(digest) - 1]}#{digest}"


async def _expire_older(db: AsyncSession, source: str, release: str) -> None:
    releases = (await db.execute(
        select(LandscapeEvidenceCache.release).distinct()
        .where(LandscapeEvidenceCache.source == source,
               LandscapeEvidenceCache.release != release))).scalars().all()
    older = [r for r in releases if _release_order(r) < _release_order(release)]
    if older:
        await db.execute(delete(LandscapeEvidenceCache).where(
            LandscapeEvidenceCache.source == source,
            LandscapeEvidenceCache.release.in_(older)))


async def cached(db: AsyncSession, source: str, release: str, key: str,
                 fetch: Callable[[], Awaitable[dict]]) -> dict:
    """The stored payload for (source, release, key), else ``await fetch()`` stored and
    returned. A fetch that raises stores nothing. A stored None (a source's "no record")
    is a hit. Storing under a release newer than one already held for the source deletes
    the older release's rows (§11.2). Writes in ``db``'s transaction; the caller commits."""
    query_key = _query_key(key)
    row = (await db.execute(
        select(LandscapeEvidenceCache.payload).where(
            LandscapeEvidenceCache.source == source,
            LandscapeEvidenceCache.release == release,
            LandscapeEvidenceCache.query_key == query_key))).first()
    if row is not None:
        return row[0]
    payload = await fetch()
    await _expire_older(db, source, release)
    stmt = insert(LandscapeEvidenceCache).values(
        source=source, release=release, query_key=query_key, payload=payload)
    await db.execute(stmt.on_conflict_do_update(
        constraint="uq_landscape_evidence_cache_source_release_query",
        set_={"payload": stmt.excluded.payload, "fetched_at": func.now()}))
    return payload
