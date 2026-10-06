"""The `industry_evidence` worker job and the score-row writer (spec 2026-10-05 §6.2).

The job asks each source in `industry_sources.registry.SOURCES` for the PI's evidence and
records what each covered: "ok", "truncated" (USPTO or ClinicalTrials.gov paging stopped
at its cap) or "unavailable:<reason>" (`SourceUnavailable`). Evidence rows are upserted
on (user_id, source, kind, external_id), every column refreshed except the id and the
veto, so a row keeps its id (the veto form's URL) and its veto across runs (E-9). Then the
non-vetoed rows that a source with coverage "ok" did not return are deleted, which also
removes rows earlier parsers produced (NEW-1). A truncated or unavailable source keeps its
unreturned rows; the scorer counts them only from the tenure start on. A key longer than
the column's 120 characters is shortened to a stable hash (`evidence_key`, E-13).

`rescore_user` writes a SCORER_VERSION row: raw_sum, components, evidence_count, coverage
and tenure_start_used, with computed_at = clock_timestamp() (now() is the transaction's
start, which would date a long job's row before a quicker one's). It writes no score,
reason, field_percentile or primary_field: the percentile and the reason are computed when
a page renders (`industry_score.industry_view`, D13).

Not a persona input (tests/unit/test_enrichment_isolation.py): neither the job nor the
veto route takes `profile_publish.lock_persona_writer`."""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, insert, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    AgentRegistry,
    PiIndustryEvidence,
    PiIndustryScore,
    Publication,
    ResearcherProfile,
    User,
)
from src.services import job_progress
from src.services.industry_score import NOT_REFRESHED, SCORER_VERSION, score_evidence
from src.services.industry_sources import EvidenceItem
from src.services.industry_sources.registry import SOURCES, SourceContext, SourceUnavailable
from src.services.jhu_rules import get_tenure_start
from src.services.tenure_scope import publication_in_use

if TYPE_CHECKING:
    from src.worker.main import JobContext

logger = logging.getLogger(__name__)

#: `pi_industry_evidence.external_id` is varchar(120); a longer key is hashed (E-13).
EXTERNAL_ID_MAX = 120
_HASHED_PREFIX = 100
#: Rows per upsert statement (12 binds a row, far under Postgres's 32,767).
_UPSERT_BATCH = 500
#: What an upsert refreshes: every column but the key, the id and the veto (spec §6.2).
_REFRESHED = ("company_name", "company_external_id", "company_class", "year", "pi_role",
              "in_tenure", "evidence")
_COVERAGE_MAX = 60
#: NOT_REFRESHED (defined in industry_score, which ranks without such rows) is what a
#: rescore records for every source when the PI has no refreshed current-version row yet (a
#: veto before the PI's first 2.0.0 job, A5): the page shows "partial", and the completeness
#: checks (the gate, scripts/verify_industry_remediation.py, `_bulk_enqueue
#: --missing-scorer-version`) count the row as not refreshed.
#: Sources with no producer yet (registry.NihReporterSource): their "ok" does not mean a
#: source answered.
NO_PRODUCER = frozenset({"nih_reporter"})


def evidence_key(external_id: str) -> str:
    """`external_id` itself when it fits the column, else its first 100 characters, ":",
    and the first 16 hex characters of its sha1 (117 characters): stable, so a re-run
    upserts the same row, and no type change (spec §6.2)."""
    if len(external_id) <= EXTERNAL_ID_MAX:
        return external_id
    digest = hashlib.sha1(external_id.encode("utf-8")).hexdigest()[:16]
    return f"{external_id[:_HASHED_PREFIX]}:{digest}"


def _deduped(items: list[EvidenceItem]) -> list[EvidenceItem]:
    """Items with storable keys, the first of each (source, kind, external_id) kept, in
    SOURCES order: a multi-row ON CONFLICT may not meet one key twice."""
    out: list[EvidenceItem] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        item = dataclasses.replace(item, external_id=evidence_key(item.external_id))
        key = (item.source, item.kind, item.external_id)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


async def _upsert_evidence(db: AsyncSession, user_id: uuid.UUID, items: list[EvidenceItem]) -> None:
    for start in range(0, len(items), _UPSERT_BATCH):
        stmt = pg_insert(PiIndustryEvidence).values([
            {"id": uuid.uuid4(), "user_id": user_id, **dataclasses.asdict(item)}
            for item in items[start:start + _UPSERT_BATCH]
        ])
        stmt = stmt.on_conflict_do_update(
            constraint="uq_pi_industry_evidence_key",
            set_={**{c: stmt.excluded[c] for c in _REFRESHED}, "fetched_at": func.now()},
        )
        await db.execute(stmt)


async def _delete_missing(
    db: AsyncSession, user_id: uuid.UUID, items: list[EvidenceItem], ok_sources: list[str],
) -> None:
    """Delete the non-vetoed rows of each source in `ok_sources` whose key `items` lacks."""
    returned: dict[str, set[tuple[str, str]]] = {}
    for item in items:
        returned.setdefault(item.source, set()).add((item.kind, item.external_id))
    for source in ok_sources:
        stmt = delete(PiIndustryEvidence).where(
            PiIndustryEvidence.user_id == user_id, PiIndustryEvidence.source == source,
            PiIndustryEvidence.vetoed_at.is_(None),
        )
        keys = returned.get(source)
        if keys:
            stmt = stmt.where(
                tuple_(PiIndustryEvidence.kind, PiIndustryEvidence.external_id).not_in(sorted(keys))
            )
        await db.execute(stmt)


async def _insert_score(
    db: AsyncSession, user_id: uuid.UUID, *, raw_sum: float | None, components: dict,
    coverage: dict[str, str], evidence_count: int, tenure_start: int | None,
) -> PiIndustryScore:
    return (await db.execute(
        insert(PiIndustryScore).values(
            id=uuid.uuid4(), user_id=user_id, raw_sum=raw_sum, components=components,
            coverage=coverage, evidence_count=evidence_count, tenure_start_used=tenure_start,
            scorer_version=SCORER_VERSION, computed_at=func.clock_timestamp(),
        ).returning(PiIndustryScore)
    )).scalar_one()


async def _latest_coverage(db: AsyncSession, user_id: uuid.UUID) -> dict[str, str]:
    """The coverage of the PI's latest current-version row; with none, or one whose coverage
    is empty (a run with no tenure start refreshed no source), every source marked
    NOT_REFRESHED (the evidence predates this scorer version's sources)."""
    value = await db.scalar(
        select(PiIndustryScore.coverage)
        .where(PiIndustryScore.user_id == user_id, PiIndustryScore.scorer_version == SCORER_VERSION)
        .order_by(PiIndustryScore.computed_at.desc(), PiIndustryScore.id.desc())
        .limit(1)
    )
    if not value:
        return {source.name: NOT_REFRESHED for source in SOURCES}
    return dict(value)


async def rescore_user(
    db: AsyncSession, user_id: uuid.UUID, tenure_start: int | None = None, *,
    coverage: dict[str, str] | None = None,
) -> PiIndustryScore:
    """Write a SCORER_VERSION row from the PI's stored evidence (module docstring).

    `tenure_start`, when omitted (the veto route has no fresh value to hand in), is re-read
    here rather than left NULL, so a post-veto rescore is never indistinguishable from an
    unscoped one. `coverage`, when omitted, is the PI's latest current-version row's (the
    veto changed the evidence, not what the sources covered), or NOT_REFRESHED for every
    source when there is none. evidence_count is the un-vetoed rows. Flushes through
    INSERT … RETURNING; never commits."""
    rows = (await db.execute(
        select(PiIndustryEvidence).where(PiIndustryEvidence.user_id == user_id)
    )).scalars().all()
    if tenure_start is None:
        agent = (await db.execute(
            select(AgentRegistry).where(AgentRegistry.user_id == user_id)
        )).scalar_one_or_none()
        tenure_start = await get_tenure_start(db, user_id, agent_id=agent.agent_id if agent else None)
    if coverage is None:
        coverage = await _latest_coverage(db, user_id)
    raw, components = score_evidence(rows, tenure_start=tenure_start)
    return await _insert_score(
        db, user_id, raw_sum=raw, components=components, coverage=coverage,
        evidence_count=sum(1 for r in rows if r.vetoed_at is None), tenure_start=tenure_start,
    )


async def _collect(ctx: JobContext, sctx: SourceContext) -> tuple[list[EvidenceItem], dict[str, str]]:
    """Every source's items and coverage. Raises SourceUnavailable when no source with a
    producer answered (an outage: the worker's retry backoff runs the job again rather than
    rescoring stale rows); the producer-less NO_PRODUCER sources' "ok" does not count."""
    items: list[EvidenceItem] = []
    coverage: dict[str, str] = {}
    for source in SOURCES:
        try:
            result = await source.fetch(sctx)
        except SourceUnavailable as exc:
            logger.warning("industry_evidence %s: %s — keeping its stored rows", sctx.user.id, exc)
            coverage[source.name] = f"unavailable:{exc.reason}"[:_COVERAGE_MAX]
            continue
        coverage[source.name] = result.coverage
        items += result.items
        if result.note:
            await job_progress.record(ctx.id, "industry1", result.note)
    if all(state.startswith("unavailable") for name, state in coverage.items() if name not in NO_PRODUCER):
        down = [name for name, state in coverage.items() if state.startswith("unavailable")]
        raise SourceUnavailable(f"every industry source unavailable: {','.join(down)}")
    return items, coverage


async def execute_industry_evidence(ctx: JobContext, db: AsyncSession) -> None:
    user_id = uuid.UUID(ctx.payload["user_id"])
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    agent = (await db.execute(select(AgentRegistry).where(AgentRegistry.user_id == user_id))).scalar_one_or_none()
    tenure_start = await get_tenure_start(db, user_id, agent_id=agent.agent_id if agent else None)
    if tenure_start is None:
        await _insert_score(db, user_id, raw_sum=None, components={}, coverage={},
                            evidence_count=0, tenure_start=None)
        await job_progress.record(ctx.id, "industry_done", "unscored: no JHU tenure start")
        return

    profile = (await db.execute(select(ResearcherProfile).where(ResearcherProfile.user_id == user_id))).scalar_one_or_none()
    keywords = set((profile.keywords or []) + (profile.key_targets or []) + (profile.disease_areas or [])) if profile else set()
    conditions = set(profile.disease_areas or []) if profile else set()
    pubs = (await db.execute(select(Publication).where(
        Publication.user_id == user_id, Publication.pmid.isnot(None), publication_in_use(),
    ))).scalars().all()
    sctx = SourceContext(user=user, tenure_start=tenure_start, keywords=keywords, conditions=conditions,
                         pmids=[p.pmid for p in pubs], year_by_pmid={p.pmid: p.year for p in pubs})

    items, coverage = await _collect(ctx, sctx)
    items = _deduped(items)
    await _upsert_evidence(db, user_id, items)
    await _delete_missing(db, user_id, items, [s for s, state in coverage.items() if state == "ok"])
    score = await rescore_user(db, user_id, tenure_start, coverage=coverage)
    unavailable = [s for s, state in coverage.items() if state.startswith("unavailable")]
    truncated = [s for s, state in coverage.items() if state == "truncated"]
    suffix = (f" unavailable={','.join(unavailable)}" if unavailable else "") + (
        f" truncated={','.join(truncated)}" if truncated else "")
    await job_progress.record(
        ctx.id, "industry_done",
        f"evidence={score.evidence_count} raw={score.raw_sum} v{SCORER_VERSION}{suffix}",
    )
