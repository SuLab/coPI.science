"""ORCID fundings -> pi_orcid_fundings (spec 2026-10-05 §6.1 "ORCID fundings"; D4, D8, D43).

One row per ORCID funding GROUP; the summary with the lowest display-index supplies the
fields. `group_key` is the group's own grant identifier (a `self` or relationship-less id,
type `grant_number` preferred), else "hash:" + sha1 of the normalised title and funder; a
key over 200 characters is hashed. A veto follows its funding across a key change when
title, funder and any self id still match (`_carried_vetoes`). Titles are read from
group[].funding-summary[].title.title.value, dates from the start-date/year/value shape
`orcid.fetch_orcid_profile` reads for employments; every chain is null-safe, `(x.get(k) or {})` (G-20).

Two fetch modes: strict (the enrich_grants job and the repair) raises on anything but a
record-state answer, so the job retries; soft (pipeline step 2) returns None on failure
and the caller keeps the stored rows. A record-state answer (301/404/409/410,
`orcid._RECORD_STATE_STATUSES`) means "no fundings" in both.
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import PiGrantIdentity, PiOrcidFunding
from src.services import orcid
from src.services.grant_sections import normalise_title
from src.services.http_pacing import TRANSIENT_STATUSES, with_retries

logger = logging.getLogger(__name__)

GROUP_KEY_MAX = 200
#: Years outside this range read as unknown (A27), so date arithmetic downstream cannot raise.
YEAR_RANGE = range(1900, 2101)
_NON_ID = re.compile(r"[^0-9A-Za-z]")


@dataclass(frozen=True)
class OrcidFunding:
    group_key: str
    title: str
    funder_name: str | None
    funding_type: str | None
    start_year: int | None
    start_month: int | None
    end_year: int | None
    end_month: int | None
    external_ids: tuple[dict, ...]     # ({"type", "value", "relationship"}, ...)


def _value(block) -> str | None:
    value = block.get("value") if isinstance(block, dict) else None
    text = str(value).strip() if value is not None else ""
    return text or None


def _int(block) -> int | None:
    text = _value(block)
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def _date(summary: dict, key: str) -> tuple[int | None, int | None]:
    """(year, month); a year outside 1900-2100 reads as unknown, so date() and the
    month-end arithmetic downstream cannot raise (A27). A month without a year, or
    outside 1-12, reads as unknown."""
    part = summary.get(key) or {}
    year, month = _int(part.get("year")), _int(part.get("month"))
    if year is not None and year not in YEAR_RANGE:
        year = None
    if year is None or month is None or not 1 <= month <= 12:
        month = None
    return year, month


def _external_ids(container) -> list[dict]:
    out = []
    raw = container.get("external-id") if isinstance(container, dict) else None
    for eid in raw or []:
        if not isinstance(eid, dict):
            continue
        value = str(eid.get("external-id-value") or "").strip()
        if value:
            out.append({
                "type": str(eid.get("external-id-type") or "").strip().lower(),
                "value": value,
                "relationship": str(eid.get("external-id-relationship") or "").strip().lower()
                or None,
            })
    return out


def _display_index(summary: dict) -> int:
    try:
        return int(summary.get("display-index"))
    except (TypeError, ValueError):
        return 999


def _id_value(value) -> str:
    return _NON_ID.sub("", str(value or "")).upper()


def _title_hash(title: str | None, funder: str | None) -> str:
    basis = f"{normalise_title(title or '')}|{normalise_title(funder or '')}"
    return hashlib.sha1(basis.encode()).hexdigest()


def group_key(title: str, funder: str | None, ids: list[dict]) -> str:
    """The upsert key, from the self (or relationship-less) ids only, so a part-of or
    funded-by id cannot merge distinct groups: `ext:grant_number:<id>`, else
    `ext:<type>:<id>` of the first such id, else `hash:<sha1>` of normalised title and
    funder. Ids are reduced to `[0-9A-Z]`; a key over GROUP_KEY_MAX becomes
    `ext:sha1:<sha1>`."""
    own = [e for e in ids if e["relationship"] in (None, "self")]
    grant_numbers = sorted(_id_value(e["value"]) for e in own if e["type"] == "grant_number")
    self_ids = sorted(f'{e["type"]}:{_id_value(e["value"])}' for e in own)
    grant_numbers = [g for g in grant_numbers if g]
    if grant_numbers:
        key = f"ext:grant_number:{grant_numbers[0]}"
    elif self_ids:
        key = f"ext:{self_ids[0]}"
    else:
        key = f"hash:{_title_hash(title, funder)}"
    if len(key) > GROUP_KEY_MAX:
        key = "ext:sha1:" + hashlib.sha1(key.encode()).hexdigest()
    return key


def _clean(raw, limit: int) -> str | None:
    """Whitespace-collapsed text cut to the column width; None when blank or not text."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    return " ".join(raw.split())[:limit]


def parse_orcid_fundings(data: dict | None) -> list[OrcidFunding]:
    """One OrcidFunding per group of an ORCID `/fundings` body; a group without a
    title is skipped. Never raises on a malformed or partly-null body."""
    out: list[OrcidFunding] = []
    groups = data.get("group") if isinstance(data, dict) else None
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        summaries = [s for s in (group.get("funding-summary") or []) if isinstance(s, dict)]
        if not summaries:
            continue
        summary = min(summaries, key=_display_index)
        title = " ".join((_value((summary.get("title") or {}).get("title")) or "").split())
        if not title:
            continue
        funder = _clean((summary.get("organization") or {}).get("name"), 300)
        funding_type = (_clean(summary.get("type"), 40) or "").lower() or None
        ids = _external_ids(group.get("external-ids")) or _external_ids(summary.get("external-ids"))
        start_year, start_month = _date(summary, "start-date")
        end_year, end_month = _date(summary, "end-date")
        out.append(OrcidFunding(group_key(title, funder, ids), title, funder, funding_type,
                                start_year, start_month, end_year, end_month, tuple(ids)))
    return out


async def fetch_orcid_fundings(orcid_id: str, *, strict: bool) -> list[OrcidFunding] | None:
    """The PI's ORCID fundings. A record-state answer (301/404/409/410) is `[]`; any
    other failure (transport error, 429/5xx after retries, other status, unreadable
    body) raises when `strict` and returns None when not."""
    url = f"{orcid.ORCID_API_BASE}/{orcid_id}/fundings"
    headers = {"Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await with_retries(
                lambda: client.get(url, headers=headers),
                attempts=3, backoff=lambda a: 1.0 * (2 ** a),
                retry_statuses=TRANSIENT_STATUSES, retry_exceptions=(httpx.TransportError,),
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in orcid._RECORD_STATE_STATUSES:
            logger.warning("ORCID answered %d for %s's fundings; reading it as none",
                           exc.response.status_code, orcid_id)
            return []
        if strict:
            raise
        logger.warning("ORCID fundings fetch failed for %s: %s; keeping stored rows",
                       orcid_id, exc)
        return None
    except Exception as exc:
        if strict:
            raise
        logger.warning("ORCID fundings fetch failed for %s: %s; keeping stored rows",
                       orcid_id, exc)
        return None
    return parse_orcid_fundings(data if isinstance(data, dict) else None)


def _end_order(f: OrcidFunding) -> tuple[int, int, int, int]:
    return (f.end_year or 0, f.end_month or 0, f.start_year or 0, f.start_month or 0)


def _self_id_values(ids) -> set[str]:
    """The reduced values of the ``self`` and relationship-less ids (as ``group_key``
    reads them): a part-of or funded-by id is shared by distinct fundings."""
    return {
        v for e in ids or []
        if isinstance(e, dict) and e.get("relationship") in (None, "self")
        and (v := _id_value(e.get("value")))
    }


def _inherits_veto(new: OrcidFunding, new_ids: set[str], old: PiOrcidFunding) -> bool:
    """Whether ``new`` is the same funding as the orphaned vetoed row ``old``.

    An old row with self (or relationship-less) ids is the same funding exactly when it
    shares one of them with ``new``: a grant number identifies the award even if its title
    or funder was corrected. An old row without self ids (keyed by its title hash) is the
    same funding when ``new`` has the same non-empty normalised title and funder, which is
    the case of a PI adding a grant number to an award staff vetoed. part-of and
    funded-by ids never count (`_self_id_values`), so a sibling award under one centre
    grant does not inherit."""
    old_ids = _self_id_values(old.external_ids)
    if old_ids:
        return bool(old_ids & new_ids)
    return bool(normalise_title(new.title or "")) and (
        _title_hash(old.title, old.funder_name) == _title_hash(new.title, new.funder_name)
    )


async def _carried_vetoes(db: AsyncSession, user_id: uuid.UUID,
                          latest: dict[str, OrcidFunding]) -> dict[str, PiOrcidFunding]:
    """group_key -> the vetoed row whose veto that new key inherits.

    An incoming funding whose key has no row inherits the veto of an orphaned vetoed
    row (its key not returned) that ``_inherits_veto`` matches: a shared self or
    relationship-less id when the old row had one, else the same non-empty normalised
    title and funder (a funding vetoed under a ``hash:`` key that later gained a grant
    number). Each old row is claimed at most once."""
    # FOR UPDATE: a veto committed after this read would otherwise miss the carry.
    existing = (await db.execute(
        select(PiOrcidFunding).where(PiOrcidFunding.user_id == user_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().all()
    keys = {r.group_key for r in existing}
    orphans = [r for r in existing if r.vetoed_at is not None and r.group_key not in latest]
    carried: dict[str, PiOrcidFunding] = {}
    for key, f in latest.items():
        if key in keys or not orphans:
            continue
        new_ids = _self_id_values(f.external_ids)
        for old in orphans:
            if _inherits_veto(f, new_ids, old):
                carried[key] = old
                orphans.remove(old)
                break
    return carried


async def store_orcid_fundings(db: AsyncSession, user_id: uuid.UUID,
                               fundings: Sequence[OrcidFunding]) -> None:
    """Replace the PI's non-vetoed ORCID rows with `fundings` and stamp
    `pi_grant_identity.orcid_fetched_at` (inserting that row, status NULL, when absent).

    Fundings sharing a group_key collapse first (latest end wins): a multi-row
    ON CONFLICT that hits one key twice raises CardinalityViolation. The upsert
    refreshes the ORCID fields only, so a vetoed row keeps its veto; a funding whose
    key changed takes over the veto of the old vetoed row `_carried_vetoes` matches it
    to, and that row is deleted. Flushes, never commits."""
    latest: dict[str, OrcidFunding] = {}
    for f in fundings:
        if f.group_key not in latest or _end_order(f) > _end_order(latest[f.group_key]):
            latest[f.group_key] = f
    now = datetime.now(UTC)
    carried = await _carried_vetoes(db, user_id, latest) if latest else {}
    vetoes = {k: (r.vetoed_at, r.vetoed_by_user_id) for k, r in carried.items()}
    if carried:
        await db.execute(delete(PiOrcidFunding).where(
            PiOrcidFunding.id.in_([r.id for r in carried.values()])))
    if latest:
        stmt = pg_insert(PiOrcidFunding).values([
            {"id": uuid.uuid4(), "user_id": user_id, "group_key": f.group_key, "title": f.title,
             "funder_name": f.funder_name, "funding_type": f.funding_type,
             "start_year": f.start_year, "start_month": f.start_month,
             "end_year": f.end_year, "end_month": f.end_month,
             "external_ids": list(f.external_ids) or None, "fetched_at": now,
             "vetoed_at": vetoes.get(f.group_key, (None, None))[0],
             "vetoed_by_user_id": vetoes.get(f.group_key, (None, None))[1]}
            for f in latest.values()
        ])
        refreshed = ("title", "funder_name", "funding_type", "start_year", "start_month",
                     "end_year", "end_month", "external_ids", "fetched_at")
        await db.execute(stmt.on_conflict_do_update(
            constraint="uq_pi_orcid_fundings_user_group",
            set_={c: getattr(stmt.excluded, c) for c in refreshed},
        ))
    stale = delete(PiOrcidFunding).where(
        PiOrcidFunding.user_id == user_id, PiOrcidFunding.vetoed_at.is_(None)
    )
    if latest:
        stale = stale.where(PiOrcidFunding.group_key.notin_(list(latest)))
    await db.execute(stale)
    await db.execute(
        pg_insert(PiGrantIdentity).values(user_id=user_id, orcid_fetched_at=now)
        .on_conflict_do_update(index_elements=["user_id"], set_={"orcid_fetched_at": now})
    )
    await db.flush()
