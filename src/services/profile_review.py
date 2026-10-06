"""Staff review of a PI's corpus and staged draft (spec 2026-10-05 §6.3 Review UIs, D21,
D45, D47, D71): the manager PI page's Candidate papers, Unanchored papers (with Restore for
excluded rows), Pending draft, Regenerate and persona Re-export. Each action runs in the
caller's transaction under the persona writer locks; the router commits and then writes the
persona file."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Job, Publication, PublicationCandidate, ResearcherProfile
from src.models.publication import PROVENANCE_MANUAL, PROVENANCE_UNANCHORED
from src.services.corpus_additions import lock_corpus, reconcile_record_doi
from src.services.profile_drafts import DRAFT_FIELDS, Draft, draft_is_stale, read_draft
from src.services.profile_limits import SUMMARY_MAX_CHARS
from src.services.profile_publish import persona_out_of_date

#: How long after a completed generation Regenerate stays refused (spec §6.3).
REGENERATE_COOLDOWN = timedelta(hours=1)

_IN_FLIGHT = ("pending", "processing")


@dataclass(frozen=True)
class ReviewCards:
    candidates: list[PublicationCandidate]      # pending, newest year first
    unanchored: list[Publication]               # provenance 'unanchored', not excluded
    excluded: list[Publication]                 # staff-excluded rows, offered Restore (D71)
    draft: Draft | None
    draft_stale: bool
    draft_diff: list[tuple[str, Any, Any]]      # (field, stored, draft) for differing fields
    regenerate_refusal: str | None              # None = allowed; else the reason shown
    persona_out_of_date: bool | None


async def _generation_in_flight(db: AsyncSession, user_id: uuid.UUID) -> bool:
    return await db.scalar(
        select(Job.id).where(
            Job.user_id == user_id, Job.type == "generate_profile", Job.status.in_(_IN_FLIGHT)
        ).limit(1)
    ) is not None


async def regenerate_refusal(db: AsyncSession, user_id: uuid.UUID) -> str | None:
    """Why Regenerate is refused now, or None: a ``generate_profile`` job is pending or
    processing, or the newest completed one finished within ``REGENERATE_COOLDOWN``."""
    if await _generation_in_flight(db, user_id):
        return "A profile generation is already queued or running."
    last = await db.scalar(
        select(func.max(Job.completed_at)).where(
            Job.user_id == user_id, Job.type == "generate_profile", Job.status == "completed"
        )
    )
    if last is not None and last > datetime.now(UTC) - REGENERATE_COOLDOWN:
        return (
            "A profile generation finished less than an hour ago "
            f"(at {last.astimezone(UTC):%H:%M} UTC); try again later."
        )
    return None


def _draft_diff(profile: ResearcherProfile, draft: Draft) -> list[tuple[str, Any, Any]]:
    # None, "" and [] all read as "empty", so a field absent on both sides is no difference.
    return [
        (f, getattr(profile, f), draft.fields.get(f))
        for f in DRAFT_FIELDS
        if (getattr(profile, f) or None) != (draft.fields.get(f) or None)
    ]


async def load_review_cards(db: AsyncSession, user_id: uuid.UUID) -> ReviewCards:
    """Everything the staff review cards show for the PI. Reads only."""
    candidates = (await db.execute(
        select(PublicationCandidate)
        .where(PublicationCandidate.user_id == user_id, PublicationCandidate.status == "pending")
        .order_by(PublicationCandidate.year.desc().nullslast(), PublicationCandidate.pmid)
    )).scalars().all()
    unanchored = (await db.execute(
        select(Publication)
        .where(
            Publication.user_id == user_id,
            Publication.provenance == PROVENANCE_UNANCHORED,
            Publication.excluded_at.is_(None),
        )
        .order_by(Publication.year.desc().nullslast(), Publication.pmid)
    )).scalars().all()
    excluded = (await db.execute(
        select(Publication)
        .where(Publication.user_id == user_id, Publication.excluded_at.is_not(None))
        .order_by(Publication.excluded_at.desc(), Publication.pmid)
    )).scalars().all()
    profile = await db.scalar(
        select(ResearcherProfile).where(ResearcherProfile.user_id == user_id)
    )
    draft = read_draft(profile) if profile is not None else None
    return ReviewCards(
        candidates=list(candidates),
        unanchored=list(unanchored),
        excluded=list(excluded),
        draft=draft,
        draft_stale=draft is not None and draft_is_stale(profile, draft),
        draft_diff=_draft_diff(profile, draft) if draft is not None else [],
        regenerate_refusal=await regenerate_refusal(db, user_id),
        persona_out_of_date=await persona_out_of_date(db, user_id),
    )


async def _pending_candidate(
    db: AsyncSession, user_id: uuid.UUID, candidate_id: uuid.UUID,
) -> PublicationCandidate | None:
    return (await db.execute(
        select(PublicationCandidate)
        .where(
            PublicationCandidate.id == candidate_id,
            PublicationCandidate.user_id == user_id,
            PublicationCandidate.status == "pending",
        )
        .with_for_update()
    )).scalar_one_or_none()


async def accept_candidate(
    db: AsyncSession, user_id: uuid.UUID, candidate_id: uuid.UUID, record: dict,
    *, actor_id: uuid.UUID,
) -> PublicationCandidate | None:
    """Store the pending candidate's paper from ``record`` (its PubMed record) with
    provenance ``manual``, or mark the already-stored row ``manual``, and mark the candidate
    accepted by ``actor_id``. None when no pending candidate of this PI has that id. Takes
    ``lock_corpus``; flushes, never commits."""
    await lock_corpus(db, user_id)
    cand = await _pending_candidate(db, user_id, candidate_id)
    if cand is None:
        return None
    row = (await db.execute(
        select(Publication)
        .where(Publication.user_id == user_id, Publication.pmid == cand.pmid)
        .with_for_update()
    )).scalar_one_or_none()
    if row is None:
        doi, doi_verified = reconcile_record_doi(record, {})
        db.add(Publication(
            user_id=user_id, pmid=cand.pmid, pmcid=record.get("pmcid"), doi=doi,
            doi_verified=doi_verified, title=record.get("title") or cand.title,
            abstract=record.get("abstract"), journal=record.get("journal"),
            year=record.get("year"), provenance=PROVENANCE_MANUAL,
        ))
    else:
        row.provenance = PROVENANCE_MANUAL
    cand.status = "accepted"
    cand.decided_by_user_id = actor_id
    cand.decided_at = datetime.now(UTC)
    await db.flush()
    return cand


async def reject_candidate(
    db: AsyncSession, user_id: uuid.UUID, candidate_id: uuid.UUID, *, actor_id: uuid.UUID,
) -> PublicationCandidate | None:
    """Mark the pending candidate rejected by ``actor_id`` (kept, so the paper is never
    offered again). None when no pending candidate of this PI has that id."""
    cand = await _pending_candidate(db, user_id, candidate_id)
    if cand is None:
        return None
    cand.status = "rejected"
    cand.decided_by_user_id = actor_id
    cand.decided_at = datetime.now(UTC)
    await db.flush()
    return cand


async def keep_publication(
    db: AsyncSession, user_id: uuid.UUID, publication_id: uuid.UUID,
) -> Publication | None:
    """Mark an unanchored, non-excluded row of this PI ``manual``. Changes no rendered text,
    so the caller records no revision. None when there is no such row."""
    row = (await db.execute(
        select(Publication)
        .where(
            Publication.id == publication_id,
            Publication.user_id == user_id,
            Publication.provenance == PROVENANCE_UNANCHORED,
            Publication.excluded_at.is_(None),
        )
        .with_for_update()
    )).scalar_one_or_none()
    if row is None:
        return None
    row.provenance = PROVENANCE_MANUAL
    await db.flush()
    return row


async def exclude_publication(
    db: AsyncSession, user_id: uuid.UUID, publication_id: uuid.UUID, *, actor_id: uuid.UUID,
) -> Publication | None:
    """Exclude a non-excluded row of this PI (kept, ignored by every reader: D45). None when
    there is no such row. Takes ``lock_corpus``; flushes."""
    await lock_corpus(db, user_id)
    row = (await db.execute(
        select(Publication)
        .where(
            Publication.id == publication_id,
            Publication.user_id == user_id,
            Publication.excluded_at.is_(None),
        )
        .with_for_update()
    )).scalar_one_or_none()
    if row is None:
        return None
    row.excluded_at = datetime.now(UTC)
    row.excluded_by_user_id = actor_id
    await db.flush()
    return row


async def restore_publication(
    db: AsyncSession, user_id: uuid.UUID, publication_id: uuid.UUID,
) -> Publication | None:
    """Undo an Exclude (D71): clear ``excluded_at`` and ``excluded_by_user_id`` on an
    excluded row of this PI. None when there is no such row. Takes ``lock_corpus``;
    flushes. The caller records the acting user on the revision."""
    await lock_corpus(db, user_id)
    row = (await db.execute(
        select(Publication)
        .where(
            Publication.id == publication_id,
            Publication.user_id == user_id,
            Publication.excluded_at.is_not(None),
        )
        .with_for_update()
    )).scalar_one_or_none()
    if row is None:
        return None
    row.excluded_at = None
    row.excluded_by_user_id = None
    await db.flush()
    return row


async def accept_draft(db: AsyncSession, user_id: uuid.UUID) -> str | None:
    """Write the staged draft into the profile (D19). None on success, else an error code:
    ``"no_draft"``, ``"profile_generating"`` (a generation is pending or processing) or
    ``"draft_stale"`` (``profile_version`` moved since the draft's run read it). Bumps the
    version, sets ``profile_generated_at = now()``, clears the draft and leaves
    ``human_edited_at`` alone. The caller records the revision and exports."""
    profile = (await db.execute(
        select(ResearcherProfile)
        .where(ResearcherProfile.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    draft = read_draft(profile) if profile is not None else None
    if draft is None:
        return "no_draft"
    if await _generation_in_flight(db, user_id):
        return "profile_generating"
    if draft_is_stale(profile, draft):
        return "draft_stale"
    # Older staged data may predate checked_synthesis's hard resource bound.
    summary = draft.fields.get("research_summary")
    if not isinstance(summary, str):
        return "no_draft"
    if len(summary) > SUMMARY_MAX_CHARS:
        return "summary_too_long"
    result = await db.execute(
        update(ResearcherProfile)
        .where(
            ResearcherProfile.id == profile.id,
            ResearcherProfile.profile_version == draft.base_profile_version,
        )
        .values(
            **{f: draft.fields.get(f) for f in DRAFT_FIELDS},
            synthesis_validated=draft.synthesis_validated,
            evidence_pmid_count=draft.evidence_pmid_count,
            evidence_pub_count=draft.evidence_pub_count,
            evidence_flagged_count=draft.evidence_flagged_count,
            profile_version=func.coalesce(ResearcherProfile.profile_version, 0) + 1,
            profile_generated_at=func.now(),
            pending_profile=None,
            pending_profile_created_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        return "draft_stale"
    # The export that follows renders from the session's objects; reload what the UPDATE wrote.
    await db.refresh(profile)
    return None


async def discard_draft(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """Clear the staged draft. False when there was none."""
    profile = (await db.execute(
        select(ResearcherProfile)
        .where(ResearcherProfile.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if profile is None or (
        profile.pending_profile is None and profile.pending_profile_created_at is None
    ):
        return False
    profile.pending_profile = None
    profile.pending_profile_created_at = None
    await db.flush()
    return True
