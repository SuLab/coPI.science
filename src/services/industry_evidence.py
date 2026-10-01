"""Worker handler for the ``industry_evidence`` job + rescoring helper."""
from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select
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
from src.services.industry_score import SCORER_VERSION, normalise, score_evidence
from src.services.industry_sources.registry import SOURCES, SourceContext, SourceUnavailable
from src.services.jhu_rules import get_tenure_start

if TYPE_CHECKING:
    from src.worker.main import JobContext

logger = logging.getLogger(__name__)


_MIN_COHORT_PEERS = 3


async def rescore_user(db: AsyncSession, user_id: uuid.UUID, tenure_start: int | None = None, primary_field: str | None = None) -> PiIndustryScore:
    """Recompute and store a PI's industry score.

    ``tenure_start``, when omitted (as the veto route does — it has no fresh
    pipeline-derived value to hand in), is re-read here rather than left
    NULL, so a post-veto rescore is never indistinguishable from an
    unscoped one.

    A PI with no live evidence is ``score=None, reason="no_evidence"`` —
    never a numeric percentile, which a cohort of all-zero peers would
    otherwise produce (bisect_right always finds the top of an all-[0.0]
    cohort). A PI who DOES have evidence but fewer than
    ``_MIN_COHORT_PEERS`` other scored PIs at this scorer version is
    ``score=None, reason="cohort_too_small"`` — raw_sum/components are still
    recorded so nothing about the collected evidence is lost.
    """
    rows = (await db.execute(select(PiIndustryEvidence).where(PiIndustryEvidence.user_id == user_id))).scalars().all()
    raw, comps = score_evidence(rows)
    live = [r for r in rows if r.vetoed_at is None]

    if tenure_start is None:
        agent = (await db.execute(select(AgentRegistry).where(AgentRegistry.user_id == user_id))).scalar_one_or_none()
        tenure_start = await get_tenure_start(db, user_id, agent_id=agent.agent_id if agent else None)

    if not live:
        score = PiIndustryScore(user_id=user_id, score=None, raw_sum=raw, reason="no_evidence",
                                components=comps, primary_field=primary_field, tenure_start_used=tenure_start,
                                evidence_count=0, scorer_version=SCORER_VERSION)
        db.add(score)
        await db.flush()
        return score

    # Pick each OTHER user's LATEST row at this scorer_version FIRST (the
    # row_number/partition below), THEN decide whether it counts as a peer —
    # filtering on raw_sum/reason before picking "latest" would let an older,
    # stale row stand in for a user whose newest row should have won.
    # "ok" and "cohort_too_small" are the two reasons whose raw_sum reflects
    # real, live evidence (score_evidence ran); "no_evidence" and
    # "no_tenure_start" both carry a raw_sum of 0/NULL that is not a genuine
    # data point and must not pad the cohort — the failure this fix closes
    # was exactly that: enough "no_evidence" (raw_sum=0.0, so NOT NULL)
    # peers could satisfy the >=3 minimum and get normalised against an
    # effectively empty cohort. Requiring raw_sum > 0 in addition to the
    # reason allowlist is a second, redundant guard against the same class
    # of degenerate row.
    ranked = (
        select(
            PiIndustryScore.raw_sum,
            PiIndustryScore.reason,
            func.row_number().over(
                partition_by=PiIndustryScore.user_id,
                order_by=PiIndustryScore.computed_at.desc(),
            ).label("rn"),
        )
        .where(PiIndustryScore.user_id != user_id, PiIndustryScore.scorer_version == SCORER_VERSION)
        .subquery()
    )
    latest = (await db.execute(
        select(ranked.c.raw_sum).where(
            ranked.c.rn == 1,
            ranked.c.reason.in_(("ok", "cohort_too_small")),
            ranked.c.raw_sum.isnot(None),
            ranked.c.raw_sum > 0,
        ))).all()
    peer_raws = [r for (r,) in latest]

    if len(peer_raws) < _MIN_COHORT_PEERS:
        score = PiIndustryScore(user_id=user_id, score=None, raw_sum=raw, reason="cohort_too_small",
                                components=comps, primary_field=primary_field, tenure_start_used=tenure_start,
                                evidence_count=len(live), scorer_version=SCORER_VERSION)
        db.add(score)
        await db.flush()
        return score

    cohort = peer_raws + [raw]
    score = PiIndustryScore(user_id=user_id, score=normalise(raw, cohort), raw_sum=raw, reason="ok",
                            components=comps, primary_field=primary_field, tenure_start_used=tenure_start,
                            evidence_count=len(live), scorer_version=SCORER_VERSION)
    db.add(score)
    await db.flush()
    return score


async def execute_industry_evidence(ctx: JobContext, db: AsyncSession) -> None:
    user_id = uuid.UUID(ctx.payload["user_id"])
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    agent = (await db.execute(select(AgentRegistry).where(AgentRegistry.user_id == user_id))).scalar_one_or_none()
    tenure_start = await get_tenure_start(db, user_id, agent_id=agent.agent_id if agent else None)
    if tenure_start is None:
        db.add(PiIndustryScore(user_id=user_id, score=None, reason="no_tenure_start", components={}, scorer_version=SCORER_VERSION))
        await db.flush()
        await job_progress.record(ctx.id, "industry_done", "unscored: no JHU tenure start")
        return

    profile = (await db.execute(select(ResearcherProfile).where(ResearcherProfile.user_id == user_id))).scalar_one_or_none()
    keywords = set((profile.keywords or []) + (profile.key_targets or []) + (profile.disease_areas or [])) if profile else set()
    conditions = set(profile.disease_areas or []) if profile else set()
    pubs = (await db.execute(select(Publication).where(Publication.user_id == user_id, Publication.pmid.isnot(None)))).scalars().all()
    sctx = SourceContext(user=user, tenure_start=tenure_start, keywords=keywords, conditions=conditions,
                         pmids=[p.pmid for p in pubs], year_by_pmid={p.pmid: p.year for p in pubs})

    items, primary_field, refreshed, unavailable = [], None, [], []
    for source in SOURCES:
        try:
            result = await source.fetch(sctx)
        except SourceUnavailable as exc:
            logger.warning("industry_evidence %s: %s — keeping its stored rows", user_id, exc)
            unavailable.append(source.name)
            continue
        refreshed.append(source.name)
        items += result.items
        primary_field = primary_field or result.primary_field
        if result.note:
            await job_progress.record(ctx.id, "industry1", result.note)
    if not refreshed:
        # Nothing answered (an outage): fail the job so the worker's retry backoff
        # runs it again, rather than rescoring stale rows and calling it done.
        raise SourceUnavailable(f"every industry source unavailable: {','.join(unavailable)}")

    vetoed = {(r.source, r.kind, r.external_id) for r in (await db.execute(
        select(PiIndustryEvidence).where(PiIndustryEvidence.user_id == user_id,
                                         PiIndustryEvidence.vetoed_at.isnot(None)))).scalars().all()}
    if refreshed:
        await db.execute(delete(PiIndustryEvidence).where(
            PiIndustryEvidence.user_id == user_id,
            PiIndustryEvidence.source.in_(refreshed),
            PiIndustryEvidence.vetoed_at.is_(None),
        ))
    seen = set()
    for it in items:
        key = (it.source, it.kind, it.external_id)
        if key in seen or key in vetoed:
            continue
        seen.add(key)
        db.add(PiIndustryEvidence(user_id=user_id, **it.__dict__))
    await db.flush()
    s = await rescore_user(db, user_id, tenure_start, primary_field)
    suffix = f" unavailable={','.join(unavailable)}" if unavailable else ""
    await job_progress.record(ctx.id, "industry_done", f"evidence={s.evidence_count} raw={s.raw_sum} score={s.score} v{SCORER_VERSION}{suffix}")
