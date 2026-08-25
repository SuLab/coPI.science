"""JHU-instance tenure-window rules (jhu-instance-rules design R2, tasks J1/J4).

Profiles on this instance are scoped to IP associated with JHU: synthesized
(and exported) only from publications with ``year >= tenure_start``. Per-paper
affiliation filtering was measured and REJECTED (869 removals, 290 of them
indexing artifacts) — affiliations are used only to recognize a Hopkins
EMPLOYMENT in ORCID and to date the PI's earliest Hopkins-affiliated paper,
and the paper tier must look at the PI's OWN affiliation on the paper, never a
co-author's (audit finding H2).

Persistence (audit findings H1/M1): one AppSetting row per user
(``jhu_tenure_start:{user_id}``, JSON ``{"year", "source", "derived_at"}``),
written as an upsert so there is no read-modify-write of a shared map and no
lock to hold. Callers decide the transaction: the manager route passes its
request session (atomic with user creation); the pipeline passes a SHORT
dedicated session, never the job session, because the worker COMMITS
mid-pipeline state when a job fails — a paper-derived year from a degraded run
must not outlive the run that derived it. The 2026-08-13 curated map (legacy
key ``jhu_tenure_start``, keyed by agent_id) is read as a fallback.
"""

import json
import logging
import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AppSetting

logger = logging.getLogger(__name__)

TENURE_KEY_PREFIX = "jhu_tenure_start:"
LEGACY_TENURE_KEY = "jhu_tenure_start"

# Substring patterns (lowercased haystack) and word-bounded acronyms. The
# acronyms need boundaries: "Jhunjhunwala" contains "jhu".
_HOPKINS_SUBSTRINGS = (
    "johns hopkins",
    "bloomberg school of public health",
)
_HOPKINS_ACRONYMS = re.compile(r"\b(jhu|jhmi|jhsph)\b", re.IGNORECASE)


def is_hopkins_affiliation(text: str | None) -> bool:
    """Whether an organization/affiliation string names Johns Hopkins."""
    if not text:
        return False
    lowered = text.lower()
    if any(s in lowered for s in _HOPKINS_SUBSTRINGS):
        return True
    return bool(_HOPKINS_ACRONYMS.search(text))


def tenure_filter(pubs: list[dict], start: int | None) -> list[dict]:
    """Keep publications inside the tenure window.

    Identity when no start is known (rule J1: every rule is a no-op without a
    tenure entry). When a start IS set, undated papers are excluded — an
    unknown year cannot prove the paper is in-tenure.
    """
    if start is None:
        return pubs
    return [p for p in pubs if p.get("year") and p["year"] >= start]


def derive_employment_start(employments: list[dict]) -> int | None:
    """Tenure start from ORCID employments: the earliest CURRENT Hopkins one.

    Requiring ``current`` (no end-date) keeps ended stints — a postdoc who
    left and returned — from stretching the window across the away years; the
    earliest start among joint current appointments is the arrival year.
    """
    years = [
        e["start_year"]
        for e in employments
        if e.get("current")
        and e.get("start_year")
        and is_hopkins_affiliation(e.get("organization"))
    ]
    return min(years) if years else None


def derive_start_from_papers(records: list[dict]) -> int | None:
    """Tenure start from the corpus: earliest paper the PI wrote AT Hopkins.

    Each record must carry ``pi_affiliations`` — the affiliation strings of
    the author the corpus resolver matched as the PI herself. A paper whose
    only Hopkins author is a co-author has no Hopkins string there and cannot
    date tenure (H2). Undated papers cannot either.
    """
    years = [
        r["year"]
        for r in records
        if r.get("year")
        and any(is_hopkins_affiliation(a) for a in r.get("pi_affiliations") or [])
    ]
    return min(years) if years else None


async def get_tenure_start(
    db: AsyncSession,
    user_id: uuid.UUID,
    agent_id: str | None = None,
) -> int | None:
    """The PI's tenure-start year, or None when nothing is recorded."""
    row = await db.execute(
        select(AppSetting.value).where(
            AppSetting.key == f"{TENURE_KEY_PREFIX}{user_id}"
        )
    )
    value = row.scalar_one_or_none()
    if value:
        try:
            return int(json.loads(value)["year"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            logger.warning("Unreadable tenure entry for user %s: %r", user_id, value)

    if agent_id:
        row = await db.execute(
            select(AppSetting.value).where(AppSetting.key == LEGACY_TENURE_KEY)
        )
        legacy = row.scalar_one_or_none()
        if legacy:
            try:
                year = json.loads(legacy).get(agent_id)
                return int(year) if year is not None else None
            except (ValueError, TypeError, json.JSONDecodeError):
                logger.warning("Unreadable legacy tenure map")
    return None


async def set_tenure_start(
    user_id: uuid.UUID,
    year: int,
    source: str,
    *,
    db: AsyncSession,
) -> None:
    """Upsert the per-user tenure entry, with provenance.

    ``source`` records how the year was derived (``orcid_employment``,
    ``earliest_hopkins_paper``, ``manual``, ``curated-2026-08-13``) so a
    machine-derived entry is always distinguishable from a curated one.
    """
    value = json.dumps(
        {
            "year": int(year),
            "source": source,
            "derived_at": datetime.now(UTC).isoformat(),
        }
    )
    stmt = pg_insert(AppSetting).values(
        key=f"{TENURE_KEY_PREFIX}{user_id}", value=value
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[AppSetting.key], set_={"value": value}
    )
    await db.execute(stmt)
