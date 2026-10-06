"""The one rule for what a resolved corpus does to a PI's stored publications (spec
2026-10-05 §6.3, D14, D17, D44), shared by the profile pipeline and the corpus scripts,
plus the corpus advisory lock both writers take around publication writes (SC-6).

Storage reads the resolver's uncapped ``ranked`` list (the 50-cap bounds only the
reported ``kept`` set): only ORCID-anchored records (stage s1 or s3) are stored, first
ingest included; unanchored ones are held as pending ``publication_candidates`` for
staff review. Nothing is ever deleted (D15)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.publication import (
    CANDIDATE_REASON_NO_ANCHOR,
    PROVENANCE_MANUAL,
    PROVENANCE_UNANCHORED,
    Publication,
    PublicationCandidate,
)
from src.services.advisory_locks import entity_key_sql
from src.services.pubmed import reconcile_pub_doi

if TYPE_CHECKING:
    from src.services.corpus import CorpusResult

logger = logging.getLogger(__name__)

ANCHOR_STAGES = frozenset({"s1", "s3"})


def is_anchored(rec: Mapping[str, Any]) -> bool:
    """The record carries an ORCID anchor: stage s1 (ORCID works) or s3 (PubMed [auid])."""
    return bool(set(rec.get("stages") or []) & ANCHOR_STAGES)


def provenance_for(rec: Mapping[str, Any]) -> str:
    """The provenance a run records for ``rec``: its whole sorted stage list ("s1,s4") when
    anchored, else PROVENANCE_UNANCHORED."""
    if not is_anchored(rec):
        return PROVENANCE_UNANCHORED
    return ",".join(sorted(set(rec.get("stages") or [])))


@dataclass(frozen=True)
class CorpusAdditions:
    #: anchored records with a PMID not yet stored, in ``ranked`` order (no cap: D14)
    to_store: list[dict[str, Any]]
    #: unanchored records with a PMID not yet stored: review candidates (D17)
    review_only: list[dict[str, Any]]


def select_corpus_additions(
    ranked: Sequence[dict[str, Any]], stored_pmids: Collection[str],
) -> CorpusAdditions:
    """Split the resolver's uncapped ``ranked`` list into what to store and what to hold for
    review. Records without a PMID are ignored."""
    new_recs = [r for r in ranked if r.get("pmid") and r["pmid"] not in stored_pmids]
    return CorpusAdditions(
        to_store=[r for r in new_recs if is_anchored(r)],
        review_only=[r for r in new_recs if not is_anchored(r)],
    )


def reconcile_record_doi(
    rec: Mapping[str, Any], orcid_dois: Mapping[str, str],
) -> tuple[str | None, bool | None]:
    """(doi, doi_verified) for a resolved record: the ORCID-curated DOI is the candidate,
    checked against the PubMed record's (``pubmed.reconcile_pub_doi``). doi_verified is True
    for "ok", "filled" and "corrected", False for "unverified", None for "none"."""
    pmid = rec.get("pmid")
    assigned = orcid_dois.get(pmid) if pmid else None
    assigned = assigned or rec.get("doi")
    doi, action = reconcile_pub_doi(assigned, rec.get("doi"))
    if action == "corrected":
        logger.warning(
            "[doi-gate] pmid=%s: candidate DOI %r disagrees with PubMed record DOI %r; "
            "using authoritative",
            pmid, assigned, doi,
        )
    if action == "none":
        return doi, None
    return doi, action != "unverified"


#: The fields a metadata refresh may write (D44).
REFRESHED_FIELDS = ("title", "abstract", "journal", "year", "pmcid")


def metadata_changes(row: Publication, rec: Mapping[str, Any]) -> dict[str, Any]:
    """{field: new value} for each REFRESHED_FIELDS value the record has (not None, not "")
    that differs from the stored row's. Empty for a ``manual`` row. Pure."""
    if row.provenance == PROVENANCE_MANUAL:
        return {}
    changes: dict[str, Any] = {}
    for name in REFRESHED_FIELDS:
        value = rec.get(name)
        if value is None or value == "":
            continue
        if getattr(row, name) != value:
            changes[name] = value
    return changes


@dataclass
class CorpusStoreOutcome:
    stored: list[str] = field(default_factory=list)               # PMIDs inserted
    candidates_new: list[str] = field(default_factory=list)       # pending candidates inserted
    candidates_accepted: list[str] = field(default_factory=list)  # pending -> stored by the system
    refreshed: list[str] = field(default_factory=list)            # PMIDs whose metadata changed
    year_changes: list[tuple[str, int | None, int | None]] = field(default_factory=list)
    provenance_set: int = 0                                        # rows whose provenance changed
    first_ingest: bool = False


def _stages_text(rec: Mapping[str, Any]) -> str | None:
    return ",".join(sorted(set(rec.get("stages") or []))) or None


def _refresh_row(
    row: Publication, rec: Mapping[str, Any], orcid_dois: Mapping[str, str],
    outcome: CorpusStoreOutcome,
) -> None:
    """DOI check (``manual`` rows too, as the pipeline always did), then, unless the row is
    ``manual``, the metadata refresh and the provenance backfill."""
    doi, verified = reconcile_record_doi(rec, orcid_dois)
    if doi and row.doi != doi:
        row.doi = doi
    if doi is not None:
        row.doi_verified = verified
    if row.provenance == PROVENANCE_MANUAL:
        return
    changes = metadata_changes(row, rec)
    if changes:
        if "year" in changes:
            outcome.year_changes.append((rec["pmid"], row.year, changes["year"]))
        for name, value in changes.items():
            setattr(row, name, value)
        outcome.refreshed.append(rec["pmid"])
    provenance = provenance_for(rec)
    if row.provenance != provenance:
        row.provenance = provenance
        outcome.provenance_set += 1


def _accept_by_system(cand: PublicationCandidate, outcome: CorpusStoreOutcome) -> None:
    cand.status = "accepted"
    cand.decided_at = datetime.now(UTC)
    # decided_by_user_id stays NULL: the system, not a person, decided.
    outcome.candidates_accepted.append(cand.pmid)


def _insert_row(
    db: AsyncSession, user_id: uuid.UUID, rec: Mapping[str, Any],
    orcid_dois: Mapping[str, str], outcome: CorpusStoreOutcome,
) -> Publication:
    doi, verified = reconcile_record_doi(rec, orcid_dois)
    pub = Publication(
        user_id=user_id,
        pmid=rec.get("pmid"),
        pmcid=rec.get("pmcid"),
        doi=doi,
        doi_verified=verified,
        title=rec.get("title") or "",
        abstract=rec.get("abstract", ""),
        journal=rec.get("journal"),
        year=rec.get("year"),
        provenance=provenance_for(rec),
    )
    db.add(pub)
    outcome.stored.append(rec["pmid"])
    return pub


def _hold_for_review(
    db: AsyncSession, user_id: uuid.UUID, rec: Mapping[str, Any],
    cand: PublicationCandidate | None, outcome: CorpusStoreOutcome,
) -> None:
    """An unanchored, unstored record: a new pending candidate, or a pending one's details
    refreshed. An accepted or rejected candidate is never changed (never re-offered)."""
    if cand is None:
        db.add(PublicationCandidate(
            user_id=user_id, pmid=rec["pmid"], title=rec.get("title") or "",
            year=rec.get("year"), stages=_stages_text(rec),
            reason=CANDIDATE_REASON_NO_ANCHOR, status="pending",
        ))
        outcome.candidates_new.append(rec["pmid"])
    elif cand.status == "pending":
        cand.title = rec.get("title") or cand.title
        cand.year = rec.get("year")
        cand.stages = _stages_text(rec) or cand.stages


async def apply_corpus_result(
    db: AsyncSession, user_id: uuid.UUID, result: CorpusResult,
) -> CorpusStoreOutcome:
    """Apply one resolve to the PI's stored rows, in the caller's transaction. The caller
    holds ``lock_persona_writer`` and ``lock_corpus``. Never deletes. First ingest (no
    stored row) and later runs alike store only anchored records of ``result.ranked``;
    unanchored ones become pending candidates unless a candidate row for the PMID is
    accepted or rejected; a pending candidate now anchored is stored and marked accepted
    (no deciding user); a rejected candidate is never re-offered, and one that comes back
    anchored is stored with its row left ``rejected`` (D70); a pending candidate whose PMID
    is already stored is marked accepted. Each stored row the resolve returns gets its DOI
    reconciled (``doi``, ``doi_verified``), its metadata refreshed and its provenance set
    (``provenance_for``), except that a ``manual`` row keeps its metadata and provenance.
    One row per PMID. Flushes, never commits."""
    outcome = CorpusStoreOutcome()
    rows = (await db.execute(
        select(Publication).where(Publication.user_id == user_id)
    )).scalars().all()
    by_pmid = {p.pmid: p for p in rows if p.pmid}
    outcome.first_ingest = not rows
    candidates = {c.pmid: c for c in (await db.execute(
        select(PublicationCandidate).where(PublicationCandidate.user_id == user_id)
    )).scalars().all()}

    seen: set[str] = set()
    for rec in result.ranked:
        pmid = rec.get("pmid")
        if not pmid or pmid in seen:
            continue
        seen.add(pmid)
        row = by_pmid.get(pmid)
        cand = candidates.get(pmid)
        if row is not None:
            _refresh_row(row, rec, result.orcid_dois, outcome)
        elif is_anchored(rec):
            # A rejected candidate that comes back anchored is stored too (spec §1, §6.3;
            # D70); its row stays "rejected" as the audit trail and is never re-offered.
            by_pmid[pmid] = _insert_row(db, user_id, rec, result.orcid_dois, outcome)
        else:
            _hold_for_review(db, user_id, rec, cand, outcome)

    # A pending candidate whose PMID is now stored (by this run or by another path, such
    # as a script) is settled by the system, whether or not this resolve returned it.
    for pmid, cand in candidates.items():
        if cand.status == "pending" and pmid in by_pmid:
            _accept_by_system(cand, outcome)

    await db.flush()
    return outcome


async def lock_corpus(db: AsyncSession, user_id: uuid.UUID) -> None:
    """pg_advisory_xact_lock on corpus:<user_id>; held until the caller's commit."""
    await db.execute(
        text(f"SELECT pg_advisory_xact_lock({entity_key_sql('corpus')})"),
        {"id": str(user_id)},
    )
