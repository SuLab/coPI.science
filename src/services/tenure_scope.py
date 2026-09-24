"""One definition of "inside the JHU tenure window", for every read path.

Design R2 scopes this instance to IP associated with JHU: a publication counts
only from ``tenure_start`` onward. ``jhu_rules.tenure_filter`` has always
implemented that for the dict-shaped corpus records the synthesis step uses,
but every other surface applied the rule by hand or not at all — and the
2026-09-22 audit found it applied at two of eight ``export_profile_to_markdown``
call sites and zero of the five count surfaces (plan
``docs/plans/2026-09-22-pi-corpus-attribution-remediation-plan.md`` §3B, D16/D17).
Copying the two-line filter a ninth time is what produced six of the eight
misses, so this module owns the rule instead:

* :func:`partition_publications` — the pure rule, on ORM rows.
* :func:`tenure_start_map` — the per-user year, honouring the legacy map.
* :func:`scoped_counts` — the batch form the directory list needs.
* :func:`scoped_publications_for` — the single-user form the detail pages need.
* :func:`scoped_publications_for_export` — the persona-export form, which
  returns a :class:`TenureScopedPublications` rather than a bare list so a
  caller cannot hand ``export_profile_to_markdown`` an unfiltered corpus by
  accident. That type is the whole point: the old signature took
  ``list[Publication]``, and six callers passed one straight out of a
  ``select(Publication)``.

Three rules, stated once so no surface invents its own:

``year >= tenure_start``
    inclusive, matching ``jhu_rules.tenure_filter``.
``year IS NULL`` is EXCLUDED when a start is set
    an unknown year cannot prove a paper is in-tenure — but it is counted in
    ``undated_excluded`` and reported, never silently dropped (D19).
``tenure_start is None`` is a PASS-THROUGH
    the full career is returned, ``scoped`` is False, and the caller MUST
    badge it as unscoped rather than render an unqualified number (D20,
    owner decision 2026-09-22).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AppSetting, Publication
from src.services.jhu_rules import LEGACY_TENURE_KEY, TENURE_KEY_PREFIX

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScopedCount:
    """Publication counts for one PI, split by the tenure window."""

    tenure_start: int | None
    in_tenure: int
    before_tenure: int
    undated_excluded: int

    @property
    def scoped(self) -> bool:
        """False when no tenure year is recorded — the count is full-career."""
        return self.tenure_start is not None

    @property
    def total(self) -> int:
        return self.in_tenure + self.before_tenure + self.undated_excluded


@dataclass(frozen=True)
class ScopedPublications:
    """The in-tenure rows for one PI, plus what the window excluded."""

    tenure_start: int | None
    publications: list[Publication]
    before_tenure: int
    undated_excluded: int

    @property
    def scoped(self) -> bool:
        return self.tenure_start is not None

    @property
    def count(self) -> int:
        return len(self.publications)

    @property
    def excluded(self) -> int:
        return self.before_tenure + self.undated_excluded


@dataclass(frozen=True)
class TenureScopedPublications:
    """Publications that have PASSED the tenure window, for persona export.

    A distinct type, not a ``list``, so ``export_profile_to_markdown`` can
    refuse anything that did not come through this module. Construct it with
    :func:`scoped_publications_for_export` (async, loads and filters) or
    :func:`scope_for_export` (pure, for tests and callers that already hold
    the rows). Do not build it by hand in application code.
    """

    publications: tuple[Publication, ...]
    tenure_start: int | None

    def __iter__(self):
        return iter(self.publications)

    def __len__(self) -> int:
        return len(self.publications)

    def __bool__(self) -> bool:
        return bool(self.publications)


def partition_publications(
    publications: Iterable[Publication], tenure_start: int | None
) -> ScopedPublications:
    """Split rows into in-tenure / before-tenure / undated. The rule, once.

    Order is preserved from the input; callers that need year-DESC should sort
    before or after — this function never reorders, so a caller's ``ORDER BY``
    survives it.
    """
    rows = list(publications)
    if tenure_start is None:
        return ScopedPublications(
            tenure_start=None,
            publications=rows,
            before_tenure=0,
            undated_excluded=0,
        )
    keep: list[Publication] = []
    before = 0
    undated = 0
    for pub in rows:
        if pub.year is None:
            undated += 1
        elif pub.year >= tenure_start:
            keep.append(pub)
        else:
            before += 1
    return ScopedPublications(
        tenure_start=tenure_start,
        publications=keep,
        before_tenure=before,
        undated_excluded=undated,
    )


def scope_for_export(
    publications: Iterable[Publication], tenure_start: int | None
) -> TenureScopedPublications:
    """Pure constructor for the export type — same rule as the async form."""
    scoped = partition_publications(publications, tenure_start)
    return TenureScopedPublications(
        publications=tuple(scoped.publications), tenure_start=tenure_start
    )


def _parse_year(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        return int(json.loads(raw)["year"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


async def tenure_start_map(
    db: AsyncSession,
    user_ids: Sequence[uuid.UUID],
    agent_ids: dict[uuid.UUID, str | None] | None = None,
) -> dict[uuid.UUID, int | None]:
    """Tenure-start year per user, in ONE query, legacy map included.

    ``jhu_rules.get_tenure_start`` answers for a single user and issues up to
    two queries; a directory page needs 73 answers. The fallback ordering is
    identical to that function's — the per-user ``jhu_tenure_start:{user_id}``
    key wins, and only when it is absent or unreadable does the 2026-08-13
    curated map (``jhu_tenure_start``, keyed by ``agent_id``) apply. Missing
    that fallback would silently unscope the 62 PIs who only have a legacy
    entry, which is why ``agent_ids`` is threaded through rather than
    ignored.
    """
    if not user_ids:
        return {}
    agent_ids = agent_ids or {}
    per_user_keys = [f"{TENURE_KEY_PREFIX}{uid}" for uid in user_ids]
    rows = (
        await db.execute(
            select(AppSetting.key, AppSetting.value).where(
                or_(
                    AppSetting.key.in_(per_user_keys),
                    AppSetting.key == LEGACY_TENURE_KEY,
                )
            )
        )
    ).all()

    by_key = {key: value for key, value in rows}
    legacy_raw = by_key.get(LEGACY_TENURE_KEY)
    legacy: dict[str, int] = {}
    if legacy_raw:
        try:
            for agent_id, year in (json.loads(legacy_raw) or {}).items():
                if year is not None:
                    legacy[agent_id] = int(year)
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
            logger.warning("Unreadable legacy tenure map")

    out: dict[uuid.UUID, int | None] = {}
    for uid in user_ids:
        year = _parse_year(by_key.get(f"{TENURE_KEY_PREFIX}{uid}"))
        if year is None:
            agent_id = agent_ids.get(uid)
            if agent_id:
                year = legacy.get(agent_id)
        out[uid] = year
    return out


async def scoped_counts(
    db: AsyncSession,
    user_ids: Sequence[uuid.UUID],
    agent_ids: dict[uuid.UUID, str | None] | None = None,
) -> dict[uuid.UUID, ScopedCount]:
    """Per-user publication counts split by the tenure window, in 2 queries.

    Deliberately fetches ``(user_id, year)`` pairs and counts in Python rather
    than building a per-user ``FILTER`` clause: the tenure year comes from
    ``app_settings`` (with the legacy agent-keyed fallback), which SQL cannot
    join to without reproducing that fallback in the query. The corpus is a
    few thousand rows against a page that already eager-loads every user's
    profile, jobs and agent, so the transfer is not the cost here.
    """
    if not user_ids:
        return {}
    starts = await tenure_start_map(db, user_ids, agent_ids)
    rows = (
        await db.execute(
            select(Publication.user_id, Publication.year).where(
                Publication.user_id.in_(list(user_ids))
            )
        )
    ).all()

    tally: dict[uuid.UUID, list[int]] = {uid: [0, 0, 0] for uid in user_ids}
    for user_id, year in rows:
        bucket = tally.get(user_id)
        if bucket is None:
            continue
        start = starts.get(user_id)
        if start is None:
            bucket[0] += 1  # unscoped: everything counts as in-window
        elif year is None:
            bucket[2] += 1
        elif year >= start:
            bucket[0] += 1
        else:
            bucket[1] += 1

    return {
        uid: ScopedCount(
            tenure_start=starts.get(uid),
            in_tenure=counts[0],
            before_tenure=counts[1],
            undated_excluded=counts[2],
        )
        for uid, counts in tally.items()
    }


async def scoped_publications_for(
    db: AsyncSession,
    user_id: uuid.UUID,
    agent_id: str | None = None,
    *,
    publications: Iterable[Publication] | None = None,
) -> ScopedPublications:
    """The in-tenure rows for one PI, newest first.

    Pass ``publications`` when the caller already holds the rows (the detail
    pages load them for rendering anyway) to avoid a second SELECT; the
    tenure lookup still happens here so the rule and its fallback stay in one
    place.
    """
    starts = await tenure_start_map(db, [user_id], {user_id: agent_id})
    tenure_start = starts.get(user_id)
    if publications is None:
        publications = (
            (
                await db.execute(
                    select(Publication)
                    .where(Publication.user_id == user_id)
                    .order_by(Publication.year.desc().nullslast())
                )
            )
            .scalars()
            .all()
        )
    return partition_publications(publications, tenure_start)


async def scoped_publications_for_export(
    db: AsyncSession, user_id: uuid.UUID, agent_id: str | None = None
) -> TenureScopedPublications:
    """Load a PI's publications and return ONLY the in-tenure ones, typed.

    The single supported way to obtain the ``publications`` argument of
    ``export_profile_to_markdown``. An unfiltered top-20 in a persona file is
    exactly how pre-tenure papers reached nine agents' prompts on 2026-08-14
    (audit H3), and the engine reloads that file within one main-loop tick.
    """
    scoped = await scoped_publications_for(db, user_id, agent_id)
    return TenureScopedPublications(
        publications=tuple(scoped.publications), tenure_start=scoped.tenure_start
    )
