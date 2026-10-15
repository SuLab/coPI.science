"""Staff review of a PI's corpus and staged draft (spec 2026-10-05 §6.3 Review UIs, D21,
D45, D47, D71): the manager PI page's Candidate papers, Withheld papers, the latest
generation's notes, Unanchored papers (with Restore for excluded rows), Pending draft,
Regenerate and persona Re-export. Each action runs in the
caller's transaction under the persona writer locks; the router commits and then writes the
persona file."""
from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import Job, Publication, PublicationCandidate, ResearcherProfile
from src.models.publication import (
    CANDIDATE_REASON_ORCID_AUTHOR_MISMATCH,
    EXCLUDED_REASON_STAFF,
    PROVENANCE_MANUAL,
    PROVENANCE_UNANCHORED,
    SYSTEM_EXCLUDED_REASONS,
)
from src.services.corpus_additions import (
    bounded_publication_fields,
    exclude_pmid_less_twins,
    lock_corpus,
    reconcile_record_doi,
)
from src.services.profile_drafts import DRAFT_FIELDS, Draft, draft_is_stale, read_draft
from src.services.profile_limits import SUMMARY_MAX_CHARS, cap_tags
from src.services.profile_jobs import (
    PROFILE_REGENERATION_DAILY_CAP,
    REGENERATION_LIMIT_REASON,
    recent_profile_generation_count,
)
from src.services.profile_publish import persona_out_of_date

#: How long after a completed generation Regenerate stays refused (spec §6.3).
REGENERATE_COOLDOWN = timedelta(hours=1)

_IN_FLIGHT = ("pending", "processing")

#: The progress steps shown as "Latest generation notes" on the staff PI page (Q27, F5),
#: with their labels, in this order. The routine steps (start, step1-step10, complete) are not
#: shown; tests/unit/test_pi_profile_labels.py keeps this set equal to the pipeline's steps.
GENERATION_NOTE_LABELS: dict[str, str] = {
    "refused": "Generation refused",
    "step2_orcid_unavailable": "ORCID fundings unavailable",
    "corpus_flagged": "Papers withheld",
    "corpus_truncated": "Search incomplete",
    "sparse_corpus": "Few publications found",
    "non_pubmed_unattributed": "OpenAlex works not attributed",
    "tenure_derived": "JHU tenure start",
    "tenure_unknown": "No JHU tenure start",
    "corpus_additions": "Papers added",
    "corpus_addition_review": "New candidate papers",
    "corpus_withheld_review": "New withheld papers",
    "corpus_candidates_accepted": "Candidates stored",
    "corpus_duplicates_excluded": "Duplicate titles excluded",
    "corpus_superseded_excluded": "Earlier versions excluded",
    "corpus_candidates_superseded": "Candidates rejected as superseded",
    "non_pubmed_superseded": "Works now in PubMed excluded",
    "corpus_pmcid_cleared": "PMCIDs cleared",
    "corpus_rechecked": "Stored papers re-checked",
    "corpus_metadata_refreshed": "Papers refreshed from PubMed",
    "non_pubmed_works": "Works PubMed does not have",
    "full_career_fallback": "Full-career profile",
    "synthesis_refused": "AI model refused",
    "synthesis_failed": "AI synthesis failed",
    "validation_rejected": "New text discarded",
    "draft_staged": "Draft staged",
    "concurrent_edit_kept": "Concurrent edit kept",
    "unvalidated": "Saved although it failed the quality checks",
    "ungrounded": "Not grounded in any abstract",
}
#: Staff wording for the steps whose recorded detail is addressed to the PI or is an
#: exception text; every other shown step shows its recorded detail.
GENERATION_NOTE_STAFF_TEXT: dict[str, str] = {
    "unvalidated": "The generated text failed the quality checks on both attempts and was "
                   "saved anyway; edit it or regenerate.",
    "ungrounded": "No publication abstract reached the AI model, so the text is not grounded "
                  "in the PI's papers.",
    "synthesis_failed": "The AI call failed. The job is retried until its attempts run out; "
                        "the stored profile is unchanged.",
    "synthesis_refused": "The AI model refused to write this profile. The job stopped; the "
                         "stored profile is unchanged.",
}
#: A shown note's recorded detail is cut to this many characters.
_NOTE_DETAIL_MAX = 500


@dataclass(frozen=True)
class GenerationNote:
    step: str
    label: str
    detail: str


@dataclass(frozen=True)
class GenerationNotes:
    """The newest ``generate_profile`` job's notable progress, for staff (Q27, F5)."""

    job_status: str
    enqueued_at: datetime
    completed_at: datetime | None
    notes: list[GenerationNote]


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
    #: pending candidates with reason CANDIDATE_REASON_ORCID_AUTHOR_MISMATCH (Q8, Q26)
    withheld: list[PublicationCandidate] = field(default_factory=list)
    #: the latest generation's corpus_flagged note, shown under Withheld papers
    withheld_summary: str | None = None
    generation_notes: GenerationNotes | None = None


async def latest_generation_notes(
    db: AsyncSession, user_id: uuid.UUID,
) -> GenerationNotes | None:
    """The newest ``generate_profile`` job of the PI (by ``enqueued_at``) and its notable
    steps, in recorded order (``GENERATION_NOTE_LABELS``; staff wording from
    ``GENERATION_NOTE_STAFF_TEXT``, else the recorded detail cut to _NOTE_DETAIL_MAX). None
    when the PI has no such job. Reads only."""
    job = (await db.execute(
        select(Job)
        .where(Job.user_id == user_id, Job.type == "generate_profile")
        .order_by(Job.enqueued_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if job is None:
        return None
    payload = job.payload if isinstance(job.payload, Mapping) else {}
    notes: list[GenerationNote] = []
    for entry in payload.get("progress") or []:
        step = entry.get("step") if isinstance(entry, Mapping) else None
        label = GENERATION_NOTE_LABELS.get(step) if isinstance(step, str) else None
        if label is None:
            continue
        detail = GENERATION_NOTE_STAFF_TEXT.get(step) or str(entry.get("detail") or "")
        notes.append(GenerationNote(step=step, label=label, detail=detail[:_NOTE_DETAIL_MAX]))
    return GenerationNotes(
        job_status=job.status, enqueued_at=job.enqueued_at, completed_at=job.completed_at,
        notes=notes,
    )


async def _generation_in_flight(db: AsyncSession, user_id: uuid.UUID) -> bool:
    return await db.scalar(
        select(Job.id).where(
            Job.user_id == user_id, Job.type == "generate_profile", Job.status.in_(_IN_FLIGHT)
        ).limit(1)
    ) is not None


async def regenerate_refusal(db: AsyncSession, user_id: uuid.UUID) -> str | None:
    """Why Regenerate is refused now, or None: a ``generate_profile`` job is pending or
    processing, the newest completed one finished within ``REGENERATE_COOLDOWN``, or
    PROFILE_REGENERATION_DAILY_CAP generations were enqueued in the last 24 hours (Q25;
    ``enqueue_profile_regeneration`` stays authoritative under its lock)."""
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
    if await recent_profile_generation_count(db, user_id) >= PROFILE_REGENERATION_DAILY_CAP:
        return REGENERATION_LIMIT_REASON
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
    pending = (await db.execute(
        select(PublicationCandidate)
        .where(PublicationCandidate.user_id == user_id, PublicationCandidate.status == "pending")
        .order_by(PublicationCandidate.year.desc().nullslast(), PublicationCandidate.pmid)
    )).scalars().all()
    candidates = [c for c in pending if c.reason != CANDIDATE_REASON_ORCID_AUTHOR_MISMATCH]
    withheld = [c for c in pending if c.reason == CANDIDATE_REASON_ORCID_AUTHOR_MISMATCH]
    notes = await latest_generation_notes(db, user_id)
    withheld_summary = next(
        (n.detail for n in reversed(notes.notes) if n.step == "corpus_flagged"), None
    ) if notes is not None else None
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
        candidates=candidates,
        unanchored=list(unanchored),
        excluded=list(excluded),
        draft=draft,
        draft_stale=draft is not None and draft_is_stale(profile, draft),
        draft_diff=_draft_diff(profile, draft) if draft is not None else [],
        regenerate_refusal=await regenerate_refusal(db, user_id),
        persona_out_of_date=await persona_out_of_date(db, user_id),
        withheld=withheld,
        withheld_summary=withheld_summary,
        generation_notes=notes,
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
) -> tuple[PublicationCandidate, list[str]] | None:
    """Store the pending candidate's paper from ``record`` (its PubMed record) with
    provenance ``manual``, or mark the already-stored row ``manual``, and mark the candidate
    accepted by ``actor_id``. Returns the candidate and the DOIs (else titles) of the PMID-less
    copies it moved to Excluded papers (``corpus_additions.exclude_pmid_less_twins``), or None
    when no pending candidate of this PI has that id. Takes ``lock_corpus``; flushes, never commits. Fields are bounded to their columns (R2.13)."""
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
        bounded = bounded_publication_fields(
            pmid=cand.pmid, doi=doi, pmcid=record.get("pmcid"), journal=record.get("journal"),
        )
        if bounded["doi"] is None:
            doi_verified = None
        db.add(Publication(
            user_id=user_id, pmid=cand.pmid, pmcid=bounded["pmcid"], doi=bounded["doi"],
            doi_verified=doi_verified, title=record.get("title") or cand.title,
            abstract=record.get("abstract"), journal=bounded["journal"],
            year=record.get("year"), provenance=PROVENANCE_MANUAL,
        ))
    else:
        row.provenance = PROVENANCE_MANUAL
    cand.status = "accepted"
    cand.decided_by_user_id = actor_id
    cand.decided_at = datetime.now(UTC)
    await db.flush()
    # P8: the PMID-less copy of the paper, if stored, is excluded as its PubMed twin.
    return cand, await exclude_pmid_less_twins(db, user_id)


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
    """Exclude a non-excluded row of this PI (kept, ignored by every reader: D45); records
    reason ``staff`` (contract C3). None when there is no such row. Takes ``lock_corpus``; flushes."""
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
    row.excluded_reason = EXCLUDED_REASON_STAFF
    await db.flush()
    return row


async def restore_publication(
    db: AsyncSession, user_id: uuid.UUID, publication_id: uuid.UUID,
) -> Publication | None:
    """Undo an Exclude (D71), following the reason (contract C3): ``staff`` keeps the
    provenance; a system reason turns the row ``manual``, so later runs leave it alone; NULL
    (excluded before migration 0068) applies the old rule, ``manual`` when
    ``excluded_by_user_id`` is NULL. A staff exclusion whose excluder was since deleted keeps
    its provenance (P7). Clears ``excluded_at``, ``excluded_by_user_id`` and
    ``excluded_reason``. None when there is no such row. Takes ``lock_corpus``; flushes. The caller records the acting user on the revision."""
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
    if row.excluded_reason in SYSTEM_EXCLUDED_REASONS or (
        row.excluded_reason is None and row.excluded_by_user_id is None
    ):
        # The system excluded it (by its reason, or, before 0068, with no deciding user):
        # a person re-included it, so later runs never exclude or relabel it again.
        row.provenance = PROVENANCE_MANUAL
    row.excluded_at = None
    row.excluded_by_user_id = None
    row.excluded_reason = None
    await db.flush()
    return row


async def accept_draft(db: AsyncSession, user_id: uuid.UUID) -> str | None:
    """Write the staged draft into the profile (D19). None on success, else an error code:
    ``"no_draft"``, ``"profile_generating"`` (a generation is pending or processing) or
    ``"draft_stale"`` (``profile_version`` moved since the draft's run read it). A list field
    that is not a list refuses the draft (``"no_draft"``); each list is cut to the D24 limits
    (``cap_tags``), as a generation stores it. Bumps the
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
    lists = {}
    for name in DRAFT_FIELDS[1:]:
        value = draft.fields.get(name)
        if value is not None and not isinstance(value, list):
            return "no_draft"  # a malformed staged draft, as for a non-string summary
        lists[name] = cap_tags(value or [])
    result = await db.execute(
        update(ResearcherProfile)
        .where(
            ResearcherProfile.id == profile.id,
            ResearcherProfile.profile_version == draft.base_profile_version,
        )
        .values(
            research_summary=summary,
            **lists,
            synthesis_validated=draft.synthesis_validated,
            evidence_pmid_count=draft.evidence_pmid_count,
            evidence_pub_count=draft.evidence_pub_count,
            evidence_flagged_count=draft.evidence_flagged_count,
            full_career_fallback=draft.full_career_fallback,
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
