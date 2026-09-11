"""Worker handler for the ``enrich_grants`` job (RePORTER → pi_grants → grant_titles)."""
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import AgentRegistry, Job, PiGrant, Publication, ResearcherProfile, User
from src.services.grant_resolution import (
    derive_grant_titles,
    filter_and_collapse,
    resolve_profile_ids,
)
from src.services.jhu_rules import get_tenure_start
from src.services.nih_reporter import (
    JHU_ORG_EXACT,
    PROJECT_FIELDS,
    publications_for_cores,
    search_projects,
)
from src.services.profile_pipeline import append_job_progress

logger = logging.getLogger(__name__)


def _split_name(name: str) -> tuple[str, str]:
    parts = (name or "").split()
    return (parts[0] if parts else ""), (parts[-1] if parts else "")


async def enqueue_enrichment_jobs(db: AsyncSession, user_id: uuid.UUID, orcid: str) -> None:
    for jtype in ("enrich_grants", "industry_evidence"):
        pending = await db.execute(
            select(Job.id).where(
                Job.user_id == user_id,
                Job.type == jtype,
                Job.status.in_(("pending", "processing")),
            )
        )
        if pending.scalar_one_or_none() is None:
            db.add(Job(type=jtype, user_id=user_id, payload={"user_id": str(user_id), "orcid": orcid}))


async def execute_enrich_grants(job: Job, db: AsyncSession) -> None:
    user_id = uuid.UUID(job.payload["user_id"])
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
    agent = (
        await db.execute(select(AgentRegistry).where(AgentRegistry.user_id == user_id))
    ).scalar_one_or_none()
    tenure_start = await get_tenure_start(db, user_id, agent_id=agent.agent_id if agent else None)
    first, last = _split_name(user.name)

    append_job_progress(job, "grants1", f"RePORTER name search for {last}")
    candidates = await search_projects(
        {"pi_names": [{"last_name": last}], "org_names_exact_match": [JHU_ORG_EXACT]},
        PROJECT_FIELDS,
        max_total=2000,
    )
    if not candidates:
        append_job_progress(job, "grants_done", "no_reporter_match: no JHU projects for this surname")
        return

    corpus_pmids = {
        p
        for (p,) in (
            await db.execute(
                select(Publication.pmid).where(
                    Publication.user_id == user_id, Publication.pmid.isnot(None)
                )
            )
        ).all()
    }
    cores = sorted({r["core_project_num"] for r in candidates})
    links = await publications_for_cores(cores)
    profile_ids, evidence = resolve_profile_ids(candidates, corpus_pmids, links, first)
    if not profile_ids:
        append_job_progress(
            job,
            "grants_done",
            f"no_reporter_match: {len(cores)} candidate cores, none PMID-linked; "
            f"rejected={evidence['rejected']}",
        )
        return

    criteria = {"pi_profile_ids": sorted(profile_ids), "org_names_exact_match": [JHU_ORG_EXACT]}
    if tenure_start is not None:
        criteria["fiscal_years"] = list(range(tenure_start, datetime.now(UTC).year + 2))
    rows = await search_projects(criteria, PROJECT_FIELDS, max_total=1500)
    records, mode = filter_and_collapse(rows, profile_ids, tenure_start)

    vetoed = {
        c
        for (c,) in (
            await db.execute(
                select(PiGrant.core_project_num).where(
                    PiGrant.user_id == user_id, PiGrant.vetoed_at.isnot(None)
                )
            )
        ).all()
    }
    await db.execute(delete(PiGrant).where(PiGrant.user_id == user_id, PiGrant.vetoed_at.is_(None)))
    kept = [r for r in records if r.core_project_num not in vetoed]
    for r in kept:
        db.add(PiGrant(user_id=user_id, tenure_filter_mode=mode, identity_evidence=evidence, **r.__dict__))

    profile = (
        await db.execute(select(ResearcherProfile).where(ResearcherProfile.user_id == user_id))
    ).scalar_one_or_none()
    if profile is not None:
        profile.grant_titles = derive_grant_titles(kept)
    await db.flush()
    append_job_progress(
        job,
        "grants_done",
        f"profile_ids={sorted(profile_ids)} cores={len(kept)} mode={mode} "
        f"rows_seen={len(rows)} vetoed_kept={len(vetoed)}",
    )
