"""Worker handler for the ``industry_evidence`` job + rescoring helper."""
import logging
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    AgentRegistry,
    Job,
    PiIndustryEvidence,
    PiIndustryScore,
    Publication,
    ResearcherProfile,
    User,
)
from src.services.industry_score import SCORER_VERSION, normalise, score_evidence
from src.services.industry_sources.ctgov import evidence_from_study, fetch_jhu_industry_trials
from src.services.industry_sources.openalex_industry import (
    company_funder_ids,
    evidence_from_work,
    fetch_works_for_pmids,
)
from src.services.industry_sources.pubmed_coi import evidence_from_record
from src.services.industry_sources.uspto_inventor import (
    evidence_from_application,
    fetch_jhu_applications,
)
from src.services.jhu_rules import get_tenure_start
from src.services.profile_pipeline import append_job_progress
from src.services.pubmed import fetch_pubmed_records

logger = logging.getLogger(__name__)


def _oaid(url):
    return (url or "").rsplit("/", 1)[-1]


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

    latest = (await db.execute(
        select(PiIndustryScore.user_id, PiIndustryScore.raw_sum).distinct(PiIndustryScore.user_id)
        .where(PiIndustryScore.raw_sum.isnot(None), PiIndustryScore.user_id != user_id, PiIndustryScore.scorer_version == SCORER_VERSION)
        .order_by(PiIndustryScore.user_id, PiIndustryScore.computed_at.desc()))).all()
    peer_raws = [r for (_, r) in latest]

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


async def execute_industry_evidence(job: Job, db: AsyncSession) -> None:
    user_id = uuid.UUID(job.payload["user_id"])
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    agent = (await db.execute(select(AgentRegistry).where(AgentRegistry.user_id == user_id))).scalar_one_or_none()
    tenure_start = await get_tenure_start(db, user_id, agent_id=agent.agent_id if agent else None)
    if tenure_start is None:
        db.add(PiIndustryScore(user_id=user_id, score=None, reason="no_tenure_start", components={}, scorer_version=SCORER_VERSION))
        await db.flush()
        append_job_progress(job, "industry_done", "unscored: no JHU tenure start")
        return

    profile = (await db.execute(select(ResearcherProfile).where(ResearcherProfile.user_id == user_id))).scalar_one_or_none()
    keywords = set((profile.keywords or []) + (profile.key_targets or []) + (profile.disease_areas or [])) if profile else set()
    conditions = set(profile.disease_areas or []) if profile else set()
    pubs = (await db.execute(select(Publication).where(Publication.user_id == user_id, Publication.pmid.isnot(None)))).scalars().all()
    pmids = [p.pmid for p in pubs]
    year_by_pmid = {p.pmid: p.year for p in pubs}

    items = []
    works = await fetch_works_for_pmids(pmids)
    funder_ids = {_oaid(f.get("id")) for w in works for f in w.get("funders") or []}
    cfids = await company_funder_ids(funder_ids)
    primary_field = None
    for w in works:
        primary_field = primary_field or ((w.get("primary_topic") or {}).get("field") or {}).get("display_name")
        items += evidence_from_work(w, user.orcid, tenure_start, company_funder_ids=cfids)
    append_job_progress(job, "industry1", f"openalex works={len(works)} items={len(items)}")

    for rec in await fetch_pubmed_records(pmids):
        y = rec.get("year") or year_by_pmid.get(str(rec.get("pmid")))
        items += evidence_from_record(rec, pi_year_ok=bool(y and y >= tenure_start))
    for app in await fetch_jhu_applications(user.name):
        items += evidence_from_application(app, tenure_start, keywords)
    for st in await fetch_jhu_industry_trials(user.name):
        items += evidence_from_study(st, tenure_start, conditions)

    vetoed = {(r.source, r.kind, r.external_id) for r in (await db.execute(
        select(PiIndustryEvidence).where(PiIndustryEvidence.user_id == user_id, PiIndustryEvidence.vetoed_at.isnot(None)))).scalars().all()}
    await db.execute(delete(PiIndustryEvidence).where(PiIndustryEvidence.user_id == user_id, PiIndustryEvidence.vetoed_at.is_(None)))
    seen = set()
    for it in items:
        key = (it.source, it.kind, it.external_id)
        if key in seen or key in vetoed:
            continue
        seen.add(key)
        db.add(PiIndustryEvidence(user_id=user_id, **it.__dict__))
    await db.flush()
    s = await rescore_user(db, user_id, tenure_start, primary_field)
    append_job_progress(job, "industry_done", f"evidence={s.evidence_count} raw={s.raw_sum} score={s.score} v{SCORER_VERSION}")
