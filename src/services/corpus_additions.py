"""The one rule for what a resolved corpus does to a PI's stored publications (spec
2026-10-05 §6.3, D14, D17, D44), shared by the profile pipeline and the corpus scripts,
plus the corpus advisory lock both writers take around publication writes (SC-6).

Storage reads the resolver's uncapped ``ranked`` list (the 50-cap bounds only the
reported ``kept`` set): only ORCID-anchored records (stage s1 or s3) are stored, first
ingest included; unanchored ones are held as pending ``publication_candidates`` for
staff review. Nothing is ever deleted (D15); a stored updated or superseded version that a
stored later version supersedes (PubMed UpdateIn/UpdateOf) is excluded, which keeps the row.
``apply_non_pubmed_works`` stores a PubMed-thin PI's non-PubMed works without a PMID and without review (spec
2026-10-09 §5.3)."""

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
    PROVENANCE_OPENALEX,
    PROVENANCE_UNANCHORED,
    Publication,
    PublicationCandidate,
)
from src.services.advisory_locks import entity_key_sql
from src.services.pubmed import normalize_doi, reconcile_pub_doi

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
    superseded_excluded: list[str] = field(default_factory=list)  # PMIDs excluded as superseded
    #: pending candidates settled as rejected because the resolver dropped them as superseded
    candidates_superseded: list[str] = field(default_factory=list)
    #: DOIs (else titles) of PMID-less rows excluded because a PubMed row now has the paper
    non_pubmed_superseded: list[str] = field(default_factory=list)
    first_ingest: bool = False


def _row_anchored(row: Publication) -> bool:
    """The stored row's provenance names an anchor stage (s1 or s3)."""
    return bool(set((row.provenance or "").split(",")) & ANCHOR_STAGES)


def _superseded_pairs(result: CorpusResult) -> list[tuple[str, str]]:
    """(earlier PMID, successor PMID): the resolver's own drops (``result.superseded``, which
    includes a preprint whose ``updated_in`` names its successor) plus every ``update_of``
    link of a ranked record."""
    pairs = list(result.superseded)
    for rec in result.ranked:
        pmid = rec.get("pmid")
        pairs.extend((old, pmid) for old in rec.get("update_of") or [] if pmid)
    return pairs


def _exclude_superseded(
    result: CorpusResult, by_pmid: Mapping[str, Publication], outcome: CorpusStoreOutcome,
) -> None:
    """Exclude each stored earlier version whose strong stored successor supersedes it
    (``_superseded_pairs``). Strong: the successor's ranked record is ORCID-anchored, or its
    row is ``manual`` (a person vouched for it), or, when it is not in ``ranked``, its row's
    provenance is anchored. A ``manual`` earlier version is never touched, since a person
    kept it. An anchored earlier version yields only to a strong successor, never to an
    unanchored one (a review candidate at most); a ``manual`` successor counts, so a staff
    Accept of a journal version retires its anchored preprint at the next run.
    ``excluded_by_user_id`` stays NULL: the system decided, and a staff Restore undoes it
    (and turns the row ``manual``, so later runs leave it alone)."""
    ranked = {r["pmid"]: r for r in result.ranked if r.get("pmid")}
    for old_pmid, new_pmid in _superseded_pairs(result):
        new = by_pmid.get(new_pmid)
        old = by_pmid.get(old_pmid)
        if new is None or new.excluded_at is not None or old is None or old_pmid == new_pmid:
            continue
        rec = ranked.get(new_pmid)
        new_anchored = is_anchored(rec) if rec is not None else _row_anchored(new)
        strong = new_anchored or new.provenance == PROVENANCE_MANUAL
        if not strong or old.excluded_at is not None or old.provenance == PROVENANCE_MANUAL:
            continue
        old.excluded_at = datetime.now(UTC)
        outcome.superseded_excluded.append(old_pmid)


def _settle_superseded_candidates(
    result: CorpusResult, by_pmid: Mapping[str, Publication],
    candidates: Mapping[str, PublicationCandidate], outcome: CorpusStoreOutcome,
) -> None:
    """A pending candidate for a PMID the resolver dropped as superseded, and not stored, is
    rejected by the system (no deciding user) so it is never re-offered."""
    for old_pmid, _ in result.superseded:
        cand = candidates.get(old_pmid)
        if (
            cand is not None and cand.status == "pending" and old_pmid not in by_pmid
        ):
            cand.status = "rejected"
            cand.decided_at = datetime.now(UTC)
            outcome.candidates_superseded.append(old_pmid)


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
    holds ``lock_persona_writer`` and ``lock_corpus``. Never deletes, but a stored earlier
    version that a stored published version supersedes (see
    ``_exclude_superseded``) is excluded by the system; staff Restore undoes it. A pending
    candidate the resolver dropped as superseded is rejected by the system. First ingest (no
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

    _exclude_superseded(result, by_pmid, outcome)
    _settle_superseded_candidates(result, by_pmid, candidates, outcome)
    _exclude_pmid_less_duplicates(rows, by_pmid, outcome)

    # A pending candidate whose PMID is now stored (by this run or by another path, such
    # as a script) is settled by the system, whether or not this resolve returned it.
    for pmid, cand in candidates.items():
        if cand.status == "pending" and pmid in by_pmid:
            _accept_by_system(cand, outcome)

    await db.flush()
    return outcome


@dataclass
class NonPubmedOutcome:
    stored: list[str] = field(default_factory=list)       # DOIs inserted
    stored_openalex_only: int = 0                         # of those, provenance "openalex"
    refreshed: list[str] = field(default_factory=list)    # DOIs of PMID-less rows changed
    refreshed_openalex_only: int = 0                      # of those, provenance "openalex"
    skipped: int = 0                                      # a DOI or title already stored


#: The fields a non-PubMed refresh may write, besides provenance.
_NON_PUBMED_FIELDS = ("title", "abstract", "journal", "year")


def _doi_key(doi: str) -> str:
    """A DOI as ``uq_publications_user_doi_nopmid`` compares it: prefix-stripped and
    ``lower()``-ed (not casefolded, so two keys never differ where the index sees one)."""
    return (normalize_doi(doi) or doi).lower()


def _title_key(title: str | None) -> str:
    from src.services.corpus import _normalize_title  # local: corpus imports this module

    return _normalize_title(title or "")


#: How far apart two same-titled papers' years may be and still be one paper (a preprint and
#: its journal version), for ``_exclude_pmid_less_duplicates``.
_SAME_PAPER_YEARS = 2


def _exclude_pmid_less_duplicates(
    rows: Sequence[Publication], by_pmid: Mapping[str, Publication],
    outcome: CorpusStoreOutcome,
) -> None:
    """A PMID-less row (the non-PubMed corpus, spec 2026-10-09 §5) is the same paper as an
    in-use PubMed row of the PI, indexed by PubMed since it was stored, when the two share a
    DOI, or a normalised title with years at most _SAME_PAPER_YEARS apart (so a generic
    title such as "Introduction" years later is not mistaken for it). It is excluded by the
    system (no deciding user; staff Restore undoes it), as an updated or superseded version is. A
    ``manual`` row is never touched."""
    pubmed = [p for p in by_pmid.values() if p.excluded_at is None]
    dois = {_doi_key(p.doi) for p in pubmed if p.doi}
    title_years: dict[str, list[int]] = {}
    for p in pubmed:
        if (key := _title_key(p.title)) and p.year is not None:
            title_years.setdefault(key, []).append(p.year)
    for row in rows:
        if row.pmid is not None or row.excluded_at is not None:
            continue
        if row.provenance == PROVENANCE_MANUAL:
            continue
        same_title = row.year is not None and any(
            abs(row.year - year) <= _SAME_PAPER_YEARS
            for year in title_years.get(_title_key(row.title), ())
        )
        if (row.doi and _doi_key(row.doi) in dois) or same_title:
            row.excluded_at = datetime.now(UTC)
            outcome.non_pubmed_superseded.append(row.doi or row.title or str(row.id))


def _refresh_non_pubmed(
    row: Publication, work: Mapping[str, Any], *, fields: Sequence[str] = _NON_PUBMED_FIELDS,
) -> bool:
    """Write the work's non-empty ``fields`` and its provenance onto a PMID-less, non-manual
    row. True when anything changed."""
    changed = False
    for name in (*fields, "provenance"):
        value = work.get(name)
        if value is None or value == "":
            continue
        if getattr(row, name) != value:
            setattr(row, name, value)
            changed = True
    return changed


def _replaces(row: Publication, work: Mapping[str, Any]) -> bool:
    """``work`` is a later version of the paper an in-use, PMID-less, non-``manual`` row holds
    by title: the published version over its stored preprint, across runs as within one."""
    return (
        row.pmid is None and row.provenance != PROVENANCE_MANUAL and row.excluded_at is None
        and (work.get("year") or 0) > (row.year or 0)
    )


def _note_refresh(outcome: NonPubmedOutcome, work: Mapping[str, Any]) -> None:
    outcome.refreshed.append(work["doi"])
    outcome.refreshed_openalex_only += work["provenance"] == PROVENANCE_OPENALEX


def _apply_non_pubmed_work(
    db: AsyncSession, user_id: uuid.UUID, work: Mapping[str, Any],
    by_doi: dict[str, Publication], by_title: dict[str, Publication],
    outcome: NonPubmedOutcome,
) -> None:
    """One work of ``apply_non_pubmed_works``; keeps the two indexes current as it writes."""
    key = _title_key(work["title"])
    row = by_doi.get(_doi_key(work["doi"]))
    if row is not None:
        if row.pmid is not None or row.provenance == PROVENANCE_MANUAL:
            outcome.skipped += 1
            return
        # A retitled work keeps its stored title when another row holds the new one, so no
        # two of the PI's rows share a normalised title.
        holder = by_title.get(key) if key else None
        fields = _NON_PUBMED_FIELDS if holder in (None, row) else _NON_PUBMED_FIELDS[1:]
        if _refresh_non_pubmed(row, work, fields=fields):
            _note_refresh(outcome, work)
        return
    holder = by_title.get(key) if key else None
    if holder is not None:
        if not _replaces(holder, work):
            outcome.skipped += 1
            return
        # The published version of a stored PMID-less preprint: the row takes its DOI.
        if holder.doi and by_doi.get(_doi_key(holder.doi)) is holder:
            del by_doi[_doi_key(holder.doi)]
        holder.doi = work["doi"]
        by_doi[_doi_key(work["doi"])] = holder
        _refresh_non_pubmed(holder, work)
        _note_refresh(outcome, work)
        return
    pub = Publication(
        user_id=user_id, pmid=None, doi=work["doi"], title=work["title"],
        abstract=work.get("abstract"), journal=work.get("journal"),
        year=work.get("year"), provenance=work["provenance"], doi_verified=None,
    )
    db.add(pub)
    by_doi[_doi_key(work["doi"])] = pub
    if key:
        by_title[key] = pub
    outcome.stored.append(work["doi"])
    outcome.stored_openalex_only += work["provenance"] == PROVENANCE_OPENALEX


async def apply_non_pubmed_works(
    db: AsyncSession, user_id: uuid.UUID, works: Sequence[Mapping[str, Any]],
) -> NonPubmedOutcome:
    """Store a PubMed-thin PI's non-PubMed works (spec 2026-10-09 §5.3; the records of
    ``non_pubmed_works.collect_non_pubmed_works``) in the caller's transaction, under the
    ``lock_corpus`` the caller already holds.

    A work whose DOI a stored row carries refreshes that row when it is PMID-less and not
    ``manual`` (keeping its title if another row holds the new one) and is skipped
    otherwise. A work whose normalised title a stored row has is skipped (excluded rows
    included, so a staff exclusion holds), unless that row is an in-use, PMID-less,
    non-``manual`` earlier version, which takes the work's DOI and metadata. Anything else
    is inserted with no PMID and ``doi_verified`` NULL. Never deletes. Flushes, never
    commits."""
    rows = (await db.execute(
        select(Publication).where(Publication.user_id == user_id)
        .order_by(Publication.created_at, Publication.id)
    )).scalars().all()
    by_doi: dict[str, Publication] = {}
    by_title: dict[str, Publication] = {}
    for row in rows:
        if row.doi:
            by_doi.setdefault(_doi_key(row.doi), row)
        if key := _title_key(row.title):
            by_title.setdefault(key, row)
    outcome = NonPubmedOutcome()
    for work in works:
        _apply_non_pubmed_work(db, user_id, work, by_doi, by_title, outcome)
    await db.flush()
    return outcome


async def lock_corpus(db: AsyncSession, user_id: uuid.UUID) -> None:
    """pg_advisory_xact_lock on corpus:<user_id>; held until the caller's commit."""
    await db.execute(
        text(f"SELECT pg_advisory_xact_lock({entity_key_sql('corpus')})"),
        {"id": str(user_id)},
    )
