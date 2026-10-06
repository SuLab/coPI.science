"""Persona grant sections, derived at export time (spec 2026-10-05 §6.1).

The pool is the PI's non-vetoed `pi_grants` rows whose `reporter_profile_id` is rendered
(the pinned ids when the identity is `pinned`, the accepted ids when `resolved`, none
otherwise: D9, A3) and whose activity code is research-bearing, plus the non-vetoed
`pi_orcid_fundings` rows. RePORTER rows sharing a normalised title collapse to the latest;
an ORCID row is dropped when its title matches a pooled RePORTER title or any external id
contains a pooled core number, and ORCID rows sharing a title or an id collapse likewise.
Active (D8): RePORTER `project_end` on or after today (else `last_fy` >= the current
federal FY); ORCID end month not yet over, or no end and a start within the last five
years. Active keeps the latest 15, latest end first (D59); overflow is dropped (A21). Past
exists only with a tenure start (D57): ended, dated rows whose dates fall in tenure (D58;
RePORTER by `last_fy`, A25), newest end first, at most 10 (D6), under
`## Past Grants (since <year>)`. Lines follow D7: `Title (NIH R01, 2019–2027)`.
"""
from __future__ import annotations

import calendar
import re
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentRegistry, PiGrant, PiGrantIdentity, PiOrcidFunding
from src.models.enrichment import GRANT_RENDERING_STATUSES
from src.services.grant_resolution import LLM_ACTIVITY_CODES
from src.services.jhu_rules import export_tenure_start

ACTIVE_CAP = 15
PAST_CAP = 10
ORCID_OPEN_ENDED_ACTIVE_YEARS = 5
_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_NON_ID = re.compile(r"[^0-9A-Z]")


def sections_today() -> date:
    """The day sections are computed for. A seam: the export goldens freeze it (spec §8)."""
    return date.today()


def normalise_title(title: str | None) -> str:
    """Accent-folded, casefolded, runs of non-alphanumerics as one space: the dedupe key."""
    folded = unicodedata.normalize("NFKD", title or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c)).casefold()
    return _NON_ALNUM.sub(" ", folded).strip()


def _id_key(value: object) -> str:
    return _NON_ID.sub("", str(value or "").upper())


def current_federal_fy(today: date) -> int:
    """The US federal fiscal year containing `today` (FY N runs 1 Oct N-1 to 30 Sep N)."""
    return today.year + 1 if today.month >= 10 else today.year


def _years(start: int | None, end: int | None) -> str:
    if start and end:
        return f"{start}–{end}"
    if start:
        return f"{start}–"
    if end:
        return f"–{end}"
    return ""


@dataclass(frozen=True)
class GrantLine:
    source: str                 # "nih_reporter" | "orcid"
    key: str                    # core_project_num, or "orcid:<group_key>"
    title: str                  # whitespace-collapsed
    label: str                  # "NIH R01", or the funder name ("ORCID" when blank)
    start_year: int | None
    end_year: int | None

    def render(self) -> str:
        """`Title (Label, 2019–2027)`; `Title (Label, 2019–)`; `Title (Label, –2027)`;
        `Title (Label)`."""
        years = _years(self.start_year, self.end_year)
        return f"{self.title} ({self.label}, {years})" if years else f"{self.title} ({self.label})"


@dataclass(frozen=True)
class GrantSections:
    active: tuple[GrantLine, ...]
    past: tuple[GrantLine, ...]
    #: The D58 heading year; None means no Past section (D57).
    tenure_start: int | None


EMPTY_GRANT_SECTIONS = GrantSections(active=(), past=(), tenure_start=None)


def rendered_profile_ids(identity: PiGrantIdentity | None) -> frozenset[int]:
    """The RePORTER profile ids whose awards render: the pinned ids when `pinned`, the
    accepted ids when `resolved`, none otherwise or with no identity row (D9, A3)."""
    if identity is None or identity.status not in GRANT_RENDERING_STATUSES:
        return frozenset()
    if identity.status == "pinned":
        ids = identity.pinned_profile_ids
    else:
        ids = identity.accepted_profile_ids
    return frozenset(ids or ())


def _month_end(year: int, month: int | None) -> date:
    m = month if month and 1 <= month <= 12 else 12
    return date(year, m, calendar.monthrange(year, m)[1])


def reporter_is_active(grant: PiGrant, today: date) -> bool:
    """`project_end` on or after today; with no end, `last_fy` reaches the current FY."""
    if grant.project_end is not None:
        return grant.project_end.date() >= today
    return grant.last_fy is not None and grant.last_fy >= current_federal_fy(today)


def orcid_is_active(funding: PiOrcidFunding, today: date) -> bool:
    """The end month is not over; with no end, the start is within the last five years;
    with neither, never active."""
    if funding.end_year is not None:
        return _month_end(funding.end_year, funding.end_month) >= today
    if funding.start_year is not None:
        return (funding.start_year, funding.start_month or 1) >= (
            today.year - ORCID_OPEN_ENDED_ACTIVE_YEARS, today.month)
    return False


@dataclass(frozen=True)
class _Item:
    line: GrantLine
    active: bool
    in_tenure: bool
    dated: bool
    end: date | None
    start: date | None
    ids: frozenset[str]
    title_key: str


def _clean(title: str | None) -> str:
    return " ".join((title or "").split())


def _reporter_items(identity: PiGrantIdentity | None, grants: Iterable[PiGrant], today: date,
                    tenure_start: int | None) -> list[_Item]:
    ids = rendered_profile_ids(identity)
    out = []
    for g in grants:
        if g.vetoed_at is not None or g.reporter_profile_id not in ids:
            continue
        if (g.activity_code or "") not in LLM_ACTIVITY_CODES:
            continue
        if g.project_end is not None:
            end: date | None = g.project_end.date()
        else:
            end = date(g.last_fy, 9, 30) if g.last_fy else None
        start = date(g.first_fy - 1, 10, 1) if g.first_fy else None
        end_year = g.project_end.year if g.project_end else g.last_fy
        line = GrantLine("nih_reporter", g.core_project_num, _clean(g.title),
                         f"NIH {g.activity_code}", g.first_fy, end_year)
        # A row stored under org_only, or under an older tenure year, can predate the
        # current tenure start until enrich_grants re-runs (A25).
        last_year = g.last_fy or (g.project_end.year if g.project_end else 0)
        in_tenure = tenure_start is not None and last_year >= tenure_start
        out.append(_Item(line, reporter_is_active(g, today), in_tenure, True, end, start,
                         frozenset({_id_key(g.core_project_num)}), normalise_title(g.title)))
    return out


def _orcid_in_tenure(funding: PiOrcidFunding, tenure_start: int | None) -> bool:
    """D58: the start year is in tenure; with no start, the end year is."""
    if tenure_start is None:
        return False
    if funding.start_year is not None:
        return funding.start_year >= tenure_start
    return funding.end_year is not None and funding.end_year >= tenure_start


def _orcid_items(fundings: Iterable[PiOrcidFunding], today: date,
                 tenure_start: int | None) -> list[_Item]:
    out = []
    for f in fundings:
        if f.vetoed_at is not None:
            continue
        end = _month_end(f.end_year, f.end_month) if f.end_year else None
        start = None
        if f.start_year:
            month = f.start_month if f.start_month and 1 <= f.start_month <= 12 else 1
            start = date(f.start_year, month, 1)
        values = (_id_key(e.get("value")) for e in (f.external_ids or []) if isinstance(e, dict))
        ids = frozenset(k for k in values if k)
        line = GrantLine("orcid", f"orcid:{f.group_key}", _clean(f.title),
                         _clean(f.funder_name) or "ORCID", f.start_year, f.end_year)
        out.append(_Item(line, orcid_is_active(f, today), _orcid_in_tenure(f, tenure_start),
                         start is not None or end is not None, end, start, ids,
                         normalise_title(f.title)))
    return out


def _latest(i: _Item) -> tuple:
    end = i.end or (date.max if i.start else date.min)
    return (end, i.start or date.min, i.line.key)


def _active_order(i: _Item) -> tuple:
    # Open-ended items sort first (A21).
    return (i.end or date.max, i.start or date.min, i.line.key)


def _past_order(i: _Item) -> tuple:
    return (i.end or i.start or date.min, i.start or date.min, i.line.key)


def _title_keys(i: _Item) -> set[str]:
    return {f"t:{i.title_key}"} if i.title_key else set()


def _collapse(items: list[_Item], keys_of: Callable[[_Item], set[str]]) -> list[_Item]:
    """The latest item of each group of items sharing a key."""
    kept: list[_Item] = []
    seen: set[str] = set()
    for item in sorted(items, key=_latest, reverse=True):
        keys = keys_of(item)
        if keys & seen:
            continue
        seen |= keys
        kept.append(item)
    return kept


def build_grant_sections(*, identity: PiGrantIdentity | None, grants: Sequence[PiGrant],
                         fundings: Sequence[PiOrcidFunding], tenure_start: int | None,
                         today: date) -> GrantSections:
    """The module rule on already-loaded rows; pure."""
    reporter_all = _reporter_items(identity, grants, today, tenure_start)
    reporter = _collapse(reporter_all, _title_keys)
    cores = {k for i in reporter_all for k in i.ids}
    titles = {i.title_key for i in reporter_all if i.title_key}
    orcid = [
        i for i in _orcid_items(fundings, today, tenure_start)
        if i.title_key not in titles and not any(core in oid for core in cores for oid in i.ids)
    ]
    orcid = _collapse(orcid, lambda i: _title_keys(i) | {f"x:{x}" for x in i.ids})
    pool = reporter + orcid
    active = sorted((i for i in pool if i.active), key=_active_order, reverse=True)[:ACTIVE_CAP]
    past: list[_Item] = []
    if tenure_start is not None:
        past = sorted((i for i in pool if not i.active and i.in_tenure and i.dated),
                      key=_past_order, reverse=True)[:PAST_CAP]
    return GrantSections(tuple(i.line for i in active), tuple(i.line for i in past), tenure_start)


async def load_grant_sections(db: AsyncSession, user_id: uuid.UUID,
                              today: date | None = None) -> GrantSections:
    """Load the PI's identity, grants, fundings and export tenure year, then build. Rows
    are re-read (`populate_existing`) so a post-commit writer never renders stale
    identity-map objects. `today` defaults to `sections_today()` (A2)."""
    fresh = {"populate_existing": True}
    identity = (await db.execute(
        select(PiGrantIdentity).where(PiGrantIdentity.user_id == user_id)
        .execution_options(**fresh)
    )).scalar_one_or_none()
    grants = (await db.execute(
        select(PiGrant).where(PiGrant.user_id == user_id, PiGrant.vetoed_at.is_(None))
        .execution_options(**fresh)
    )).scalars().all()
    fundings = (await db.execute(
        select(PiOrcidFunding)
        .where(PiOrcidFunding.user_id == user_id, PiOrcidFunding.vetoed_at.is_(None))
        .execution_options(**fresh)
    )).scalars().all()
    agent_id = await db.scalar(
        select(AgentRegistry.agent_id).where(AgentRegistry.user_id == user_id)
    )
    tenure_start = await export_tenure_start(db, user_id, agent_id)
    return build_grant_sections(
        identity=identity, grants=grants, fundings=fundings, tenure_start=tenure_start,
        today=today if today is not None else sections_today(),
    )


def grant_blocks(sections: GrantSections) -> list[tuple[str, tuple[GrantLine, ...]]]:
    """(heading, lines) per non-empty section, Active then Past; Past needs a tenure year."""
    blocks = []
    if sections.active:
        blocks.append(("Active Grants", sections.active))
    if sections.past and sections.tenure_start is not None:
        blocks.append((f"Past Grants (since {sections.tenure_start})", sections.past))
    return blocks


def grant_section_lines(sections: GrantSections) -> list[str]:
    """The persona's grant sections in `_bullet_section`'s shape: heading, bullets, blank."""
    lines: list[str] = []
    for heading, items in grant_blocks(sections):
        lines.append(f"## {heading}\n")
        lines.extend(f"- {item.render()}" for item in items)
        lines.append("")
    return lines
