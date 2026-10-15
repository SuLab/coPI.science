"""The one rule for what a resolved corpus does to a PI's stored publications (spec
2026-10-05 §6.3, D14, D17, D44), shared by the profile pipeline and the corpus scripts,
plus the corpus advisory lock both writers take around publication writes (SC-6).

Storage reads the resolver's uncapped ``ranked`` list (the 50-cap bounds only the
reported ``kept`` set): only ORCID-anchored records (stage s1 or s3) are stored, first
ingest included; unanchored ones are held as pending ``publication_candidates`` for
staff review. Nothing is ever deleted (D15); a stored updated or superseded version that a
stored later version supersedes (PubMed UpdateIn/UpdateOf) is excluded, which keeps the row.
Every system exclusion goes through ``exclude_by_system``, which records the reason
(``publications.excluded_reason``, contract C3) and never touches a ``manual`` or excluded row.
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
    CANDIDATE_REASON_ORCID_AUTHOR_MISMATCH,
    EXCLUDED_REASON_DUPLICATE_TITLE,
    EXCLUDED_REASON_PUBMED_TWIN,
    EXCLUDED_REASON_SUPERSEDED,
    PROVENANCE_MANUAL,
    PROVENANCE_OPENALEX,
    PROVENANCE_UNANCHORED,
    SYSTEM_EXCLUDED_REASONS,
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


#: A normalised title shorter than this never identifies a paper by itself ("Introduction",
#: "Editorial"); main SPEC PIPE-10, owner decision Q13.
TITLE_KEY_MIN_CHARS = 20
#: How far apart two same-titled papers' years may be and still be one paper (a preprint and
#: its journal version).
SAME_PAPER_YEARS = 2


def title_key(title: str | None) -> str | None:
    """``corpus._normalize_title`` of ``title`` when it has at least TITLE_KEY_MIN_CHARS
    characters, else None: a short title never identifies a paper."""
    from src.services.corpus import _normalize_title  # local: corpus imports this module

    key = _normalize_title(title or "")
    return key if len(key) >= TITLE_KEY_MIN_CHARS else None


def same_paper_by_title(
    title_a: str | None, year_a: int | None, title_b: str | None, year_b: int | None,
) -> bool:
    """Two records are one paper by title: equal ``title_key``s (so neither title is short),
    both years known, at most SAME_PAPER_YEARS apart. The one rule for the resolver's
    dedupe, the PMID-less corpus, the PubMed-twin rule and scripts/repair_pi_corpus.py."""
    key = title_key(title_a)
    return (
        key is not None and key == title_key(title_b)
        and year_a is not None and year_b is not None
        and abs(year_a - year_b) <= SAME_PAPER_YEARS
    )


def exclude_by_system(row: Publication, reason: str) -> bool:
    """Exclude an in-use, non-``manual`` row for a system ``reason`` (contract C3): sets
    ``excluded_at`` to now and ``excluded_reason``; ``excluded_by_user_id`` stays NULL, the
    system decided. Returns True when it excluded the row. A ``manual`` row (a person kept,
    accepted or restored it) or an already-excluded row is left unchanged and gives False,
    so the system never overrides a person or another exclusion's reason. Raises ValueError
    for a reason outside SYSTEM_EXCLUDED_REASONS (a person's Exclude is
    ``profile_review.exclude_publication``). Staff Restore undoes it."""
    if reason not in SYSTEM_EXCLUDED_REASONS:
        raise ValueError(f"not a system exclusion reason: {reason!r}")
    if row.provenance == PROVENANCE_MANUAL or row.excluded_at is not None:
        return False
    row.excluded_at = datetime.now(UTC)
    row.excluded_reason = reason
    return True


def incomplete_lookups_reason(result: CorpusResult) -> str | None:
    """Why this resolve's lookups are not known to be complete, or None (contract C4,
    R4.4). Only a complete resolve may relabel a row ``unanchored`` because no anchor stage
    returned it: an absence after a capped search, a dropped record or an empty ORCID list
    is no evidence. A strict-mode stage failure raises before this is reached. A
    CorpusResult that ``resolve_corpus`` did not build has no stage map and so never
    unanchors anything."""
    if result.stage_map is None:
        return "the resolve recorded no stage map"
    if result.truncated_stages:
        return f"a search stopped at its cap ({', '.join(result.truncated_stages)})"
    if result.permanently_dropped:
        return f"{len(result.permanently_dropped)} records could not be fetched"
    if result.stage_counts.get("s1", 0) == 0:
        return "the ORCID record listed no works"
    return None


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


def bounded_publication_fields(
    *, pmid: str | None, doi: str | None, pmcid: str | None, journal: str | None,
) -> dict[str, str | None]:
    """``doi``, ``pmcid`` and ``journal`` fitted to their ``publications`` columns (R2.13 of
    the 2026-10-15 publication-handling design; main PIPE-14, SCR-9; Q13; B-D4, C-D12). The
    widths are read from the model, so they cannot drift. An over-long DOI or PMCID is not
    an identifier of this paper and becomes None (one WARNING); an over-long journal is cut
    to the width (one INFO). Returns exactly the keys doi, pmcid and journal."""
    columns = Publication.__table__.c
    out: dict[str, str | None] = {"doi": doi, "pmcid": pmcid, "journal": journal}
    for name in ("doi", "pmcid"):
        value, width = out[name], columns[name].type.length
        if value is not None and len(value) > width:
            logger.warning(
                "[field-bounds] pmid=%s: %s of %d characters exceeds publications.%s (%d); "
                "storing NULL",
                pmid, name, len(value), name, width,
            )
            out[name] = None
    width = columns.journal.type.length
    if journal is not None and len(journal) > width:
        logger.info(
            "[field-bounds] pmid=%s: journal of %d characters truncated to %d",
            pmid, len(journal), width,
        )
        out["journal"] = journal[:width]
    return out


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
    #: PMIDs whose row went from "unanchored" back to an anchored stage list (C4)
    reanchored: list[str] = field(default_factory=list)
    #: PMIDs of new pending candidates with reason CANDIDATE_REASON_ORCID_AUTHOR_MISMATCH (Q8)
    withheld_new: list[str] = field(default_factory=list)
    #: PMIDs excluded, or stored already excluded, as a title duplicate of a newer paper (Q13)
    title_duplicates_excluded: list[str] = field(default_factory=list)
    #: PMIDs whose PMCID changed this run, so their stored methods text was dropped (PIPE-8)
    methods_cleared: list[str] = field(default_factory=list)
    #: PMIDs whose stored PMCID and methods text were cleared on idconv's positive "absent"
    pmcid_cleared: list[str] = field(default_factory=list)
    first_ingest: bool = False


def _row_anchored(row: Publication) -> bool:
    """The stored row's provenance names an anchor stage (s1 or s3)."""
    return bool(set((row.provenance or "").split(",")) & ANCHOR_STAGES)


def superseded_pairs(result: CorpusResult) -> list[tuple[str, str]]:
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
    (``superseded_pairs``). Strong: the successor's ranked record is ORCID-anchored, or its
    row is ``manual`` (a person vouched for it), or, when it is not in ``ranked``, its row's
    provenance is anchored. A ``manual`` earlier version is never touched, since a person
    kept it. An anchored earlier version yields only to a strong successor, never to an
    unanchored one (a review candidate at most); a ``manual`` successor counts, so a staff
    Accept of a journal version retires its anchored preprint at the next run.
    The exclusion goes through ``exclude_by_system`` with reason ``superseded`` (no deciding
    user, a ``manual`` or excluded row untouched); a staff Restore undoes it and turns the row
    ``manual``, so later runs leave it alone."""
    ranked = {r["pmid"]: r for r in result.ranked if r.get("pmid")}
    for old_pmid, new_pmid in superseded_pairs(result):
        new = by_pmid.get(new_pmid)
        old = by_pmid.get(old_pmid)
        if new is None or new.excluded_at is not None or old is None or old_pmid == new_pmid:
            continue
        rec = ranked.get(new_pmid)
        new_anchored = is_anchored(rec) if rec is not None else _row_anchored(new)
        strong = new_anchored or new.provenance == PROVENANCE_MANUAL
        if not strong:
            continue
        if exclude_by_system(old, EXCLUDED_REASON_SUPERSEDED):
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


def _hold_withheld_for_review(
    db: AsyncSession, user_id: uuid.UUID, result: CorpusResult,
    by_pmid: Mapping[str, Publication], candidates: dict[str, PublicationCandidate],
    outcome: CorpusStoreOutcome,
) -> None:
    """Q8 / C5: a flagged record the PI's ORCID record or PubMed's ORCID field lists (stage s1
    or s3) but whose author list does not name the PI, and that has no stored row, becomes a
    pending candidate with reason CANDIDATE_REASON_ORCID_AUTHOR_MISMATCH; a pending candidate
    is refreshed and takes that reason; an accepted or rejected one is never changed (D17,
    D21). On an anchored record the only flag reason is "no_individual_author_match" (R3.8)."""
    for entry in result.flagged:
        pmid = str(entry.get("pmid") or "")
        if not pmid or pmid in by_pmid or not set(entry.get("stages") or []) & ANCHOR_STAGES:
            continue
        cand = candidates.get(pmid)
        if cand is None:
            cand = PublicationCandidate(
                user_id=user_id, pmid=pmid, title=entry.get("title") or "",
                year=entry.get("year"), stages=_stages_text(entry),
                reason=CANDIDATE_REASON_ORCID_AUTHOR_MISMATCH, status="pending",
            )
            db.add(cand)
            candidates[pmid] = cand
            outcome.withheld_new.append(pmid)
        elif cand.status == "pending":
            cand.title = entry.get("title") or cand.title
            cand.year = entry.get("year")
            cand.stages = _stages_text(entry) or cand.stages
            cand.reason = CANDIDATE_REASON_ORCID_AUTHOR_MISMATCH


def _exclude_title_duplicates(
    db: AsyncSession, user_id: uuid.UUID, result: CorpusResult,
    by_pmid: dict[str, Publication], outcome: CorpusStoreOutcome,
) -> None:
    """Q13 / main PIPE-10: each ``result.title_duplicates`` pair (duplicate, keeper) whose
    keeper is stored and in use excludes the duplicate, reason ``duplicate_title``: a stored
    duplicate through ``exclude_by_system`` (a ``manual`` or excluded row is left alone), an
    unstored anchored one by storing it already excluded. An unstored unanchored duplicate is
    not stored. Staff Restore undoes it."""
    records = {str(r.get("pmid")): r for r in result.title_duplicate_records}
    for dup_pmid, keeper_pmid in result.title_duplicates:
        keeper = by_pmid.get(keeper_pmid)
        if keeper is None or keeper.excluded_at is not None:
            continue
        row = by_pmid.get(dup_pmid)
        if row is not None:
            if exclude_by_system(row, EXCLUDED_REASON_DUPLICATE_TITLE):
                outcome.title_duplicates_excluded.append(dup_pmid)
            continue
        rec = records.get(dup_pmid)
        if rec is not None and is_anchored(rec):
            by_pmid[dup_pmid] = _insert_row(
                db, user_id, rec, result.orcid_dois, outcome,
                excluded_reason=EXCLUDED_REASON_DUPLICATE_TITLE,
            )
            outcome.title_duplicates_excluded.append(dup_pmid)


def _stages_text(rec: Mapping[str, Any]) -> str | None:
    return ",".join(sorted(set(rec.get("stages") or []))) or None


def _refresh_row(
    row: Publication, rec: Mapping[str, Any], orcid_dois: Mapping[str, str],
    outcome: CorpusStoreOutcome, *, allow_unanchor: bool, pmcid_absent: Collection[str],
) -> None:
    """DOI check (``manual`` rows too, as the pipeline always did), then, unless the row is
    ``manual``, the metadata refresh and the provenance backfill. Fields are bounded to
    their columns (``bounded_publication_fields``), and a stored DOI that is not a DOI is
    cleared when this run yields none (R2.13). With ``allow_unanchor`` False
    (``incomplete_lookups_reason`` gave a reason), a row the record would make ``unanchored``
    keeps its provenance. A changed PMCID drops the stored methods text; a stored PMCID whose
    PMID is in ``pmcid_absent`` (idconv's positive "absent") is cleared with its methods text.
    ``manual`` rows keep both (D44)."""
    doi, verified = reconcile_record_doi(rec, orcid_dois)
    bounded = bounded_publication_fields(
        pmid=rec.get("pmid"), doi=doi, pmcid=rec.get("pmcid"), journal=rec.get("journal"),
    )
    if bounded["doi"] is None:
        doi, verified = None, None
    if doi and row.doi != doi:
        row.doi = doi
    if doi is not None:
        row.doi_verified = verified
    elif row.doi is not None and normalize_doi(row.doi) is None:
        logger.warning(
            "[doi-gate] pmid=%s: stored DOI %r is not a DOI; cleared", rec.get("pmid"), row.doi
        )
        row.doi = None
        row.doi_verified = None
    if row.provenance == PROVENANCE_MANUAL:
        return
    changes = metadata_changes(
        row, {**rec, "pmcid": bounded["pmcid"], "journal": bounded["journal"]}
    )
    if changes:
        if "year" in changes:
            outcome.year_changes.append((rec["pmid"], row.year, changes["year"]))
        for name, value in changes.items():
            setattr(row, name, value)
        outcome.refreshed.append(rec["pmid"])
        if "pmcid" in changes:
            # Another article's methods must not ground this paper (PIPE-8).
            row.methods_text = None
            outcome.methods_cleared.append(rec["pmid"])
    if not bounded["pmcid"] and row.pmcid is not None and rec["pmid"] in pmcid_absent:
        # idconv said positively that PMC has no article for this PMID (R4.14).
        row.pmcid = None
        row.methods_text = None
        outcome.pmcid_cleared.append(rec["pmid"])
    provenance = provenance_for(rec)
    if provenance == PROVENANCE_UNANCHORED and not allow_unanchor:
        # An incomplete lookup is no evidence that the anchor is gone (C4, R4.4).
        provenance = row.provenance
    if row.provenance != provenance:
        if (row.provenance == PROVENANCE_UNANCHORED and row.excluded_at is None
                and provenance != PROVENANCE_UNANCHORED):
            outcome.reanchored.append(rec["pmid"])
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
    *, excluded_reason: str | None = None,
) -> Publication:
    doi, verified = reconcile_record_doi(rec, orcid_dois)
    bounded = bounded_publication_fields(
        pmid=rec.get("pmid"), doi=doi, pmcid=rec.get("pmcid"), journal=rec.get("journal"),
    )
    if bounded["doi"] is None:
        verified = None
    pub = Publication(
        user_id=user_id,
        pmid=rec.get("pmid"),
        pmcid=bounded["pmcid"],
        doi=bounded["doi"],
        doi_verified=verified,
        title=rec.get("title") or "",
        abstract=rec.get("abstract", ""),
        journal=bounded["journal"],
        year=rec.get("year"),
        provenance=provenance_for(rec),
        excluded_at=datetime.now(UTC) if excluded_reason else None,
        excluded_reason=excluded_reason,
    )
    db.add(pub)
    if excluded_reason is None:
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
        cand.reason = CANDIDATE_REASON_NO_ANCHOR


async def apply_corpus_result(
    db: AsyncSession, user_id: uuid.UUID, result: CorpusResult,
    *, pmcid_absent: Collection[str] = frozenset(),
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
    One row per PMID. A row the resolve returned without an anchor is relabelled
    ``unanchored`` only when ``incomplete_lookups_reason`` is None. A title duplicate of a
    stored, in-use keeper is excluded (``_exclude_title_duplicates``). A flagged record the
    PI's ORCID lists becomes a withheld candidate (``_hold_withheld_for_review``). Every
    system exclusion goes through ``exclude_by_system`` with its reason (contract C3).
    Flushes, never commits."""
    outcome = CorpusStoreOutcome()
    rows = (await db.execute(
        select(Publication).where(Publication.user_id == user_id)
    )).scalars().all()
    by_pmid = {p.pmid: p for p in rows if p.pmid}
    outcome.first_ingest = not rows
    candidates = {c.pmid: c for c in (await db.execute(
        select(PublicationCandidate).where(PublicationCandidate.user_id == user_id)
    )).scalars().all()}

    allow_unanchor = incomplete_lookups_reason(result) is None
    seen: set[str] = set()
    for rec in result.ranked:
        pmid = rec.get("pmid")
        if not pmid or pmid in seen:
            continue
        seen.add(pmid)
        row = by_pmid.get(pmid)
        cand = candidates.get(pmid)
        if row is not None:
            _refresh_row(
                row, rec, result.orcid_dois, outcome,
                allow_unanchor=allow_unanchor, pmcid_absent=pmcid_absent,
            )
        elif is_anchored(rec):
            # A rejected candidate that comes back anchored is stored too (spec §1, §6.3;
            # D70); its row stays "rejected" as the audit trail and is never re-offered.
            by_pmid[pmid] = _insert_row(db, user_id, rec, result.orcid_dois, outcome)
        else:
            _hold_for_review(db, user_id, rec, cand, outcome)

    _exclude_title_duplicates(db, user_id, result, by_pmid, outcome)
    _exclude_superseded(result, by_pmid, outcome)
    _settle_superseded_candidates(result, by_pmid, candidates, outcome)
    _hold_withheld_for_review(db, user_id, result, by_pmid, candidates, outcome)
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
    #: DOIs excluded or stored excluded as title duplicates (Q13)
    duplicates_excluded: list[str] = field(default_factory=list)


#: The fields a non-PubMed refresh may write, besides provenance.
_NON_PUBMED_FIELDS = ("title", "abstract", "journal", "year")


def _doi_key(doi: str) -> str:
    """A DOI as ``uq_publications_user_doi_nopmid`` compares it: prefix-stripped and
    ``lower()``-ed (not casefolded, so two keys never differ where the index sees one)."""
    return (normalize_doi(doi) or doi).lower()


def _exclude_pmid_less_duplicates(
    rows: Sequence[Publication], by_pmid: Mapping[str, Publication],
    outcome: CorpusStoreOutcome,
) -> None:
    """A PMID-less row (the non-PubMed corpus, spec 2026-10-09 §5) is the same paper as an
    in-use PubMed row of the PI, indexed by PubMed since it was stored, when the two share a
    DOI, or are the same paper by title (``same_paper_by_title``: a long enough title, years
    close together), so a generic title such as "Introduction" is never mistaken for it. It
    is excluded through ``exclude_by_system`` with reason ``pubmed_twin`` (no deciding user;
    staff Restore undoes it), as an updated or superseded version is; a ``manual`` or
    excluded row is never touched."""
    pubmed = [p for p in by_pmid.values() if p.excluded_at is None]
    dois = {_doi_key(p.doi) for p in pubmed if p.doi}
    by_key: dict[str, list[Publication]] = {}
    for p in pubmed:
        if (key := title_key(p.title)) is not None:
            by_key.setdefault(key, []).append(p)
    for row in rows:
        if row.pmid is not None:
            continue
        same_title = any(
            same_paper_by_title(row.title, row.year, p.title, p.year)
            for p in by_key.get(title_key(row.title) or "", ())
        )
        if ((row.doi and _doi_key(row.doi) in dois) or same_title) and exclude_by_system(
            row, EXCLUDED_REASON_PUBMED_TWIN
        ):
            outcome.non_pubmed_superseded.append(row.doi or row.title or str(row.id))


async def exclude_pmid_less_twins(db: AsyncSession, user_id: uuid.UUID) -> list[str]:
    """P8: after a staff Accept stores a PubMed row, exclude (reason ``pubmed_twin``) the
    PI's in-use, non-``manual`` PMID-less rows that are the same paper
    (``_exclude_pmid_less_duplicates``). Returns their DOIs, else titles. The caller holds
    ``lock_corpus`` and has flushed the new row. Flushes."""
    rows = (await db.execute(
        select(Publication).where(Publication.user_id == user_id)
    )).scalars().all()
    outcome = CorpusStoreOutcome()
    _exclude_pmid_less_duplicates(rows, {p.pmid: p for p in rows if p.pmid}, outcome)
    await db.flush()
    return outcome.non_pubmed_superseded


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


def _note_refresh(outcome: NonPubmedOutcome, work: Mapping[str, Any]) -> None:
    outcome.refreshed.append(work["doi"])
    outcome.refreshed_openalex_only += work["provenance"] == PROVENANCE_OPENALEX


def _insert_non_pubmed(
    db: AsyncSession, user_id: uuid.UUID, work: Mapping[str, Any],
    by_doi: dict[str, Publication], by_title: dict[str, list[Publication]],
    outcome: NonPubmedOutcome, *, excluded: bool,
) -> Publication:
    """Insert ``work`` PMID-less, in use or already excluded as a title duplicate (Q13)."""
    pub = Publication(
        user_id=user_id, pmid=None, doi=work["doi"], title=work["title"],
        abstract=work.get("abstract"), journal=work.get("journal"),
        year=work.get("year"), provenance=work["provenance"], doi_verified=None,
        excluded_at=datetime.now(UTC) if excluded else None,
        excluded_reason=EXCLUDED_REASON_DUPLICATE_TITLE if excluded else None,
    )
    db.add(pub)
    by_doi[_doi_key(work["doi"])] = pub
    if (key := title_key(work["title"])) is not None:
        by_title.setdefault(key, []).append(pub)
    if excluded:
        outcome.duplicates_excluded.append(work["doi"])
    else:
        outcome.stored.append(work["doi"])
        outcome.stored_openalex_only += work["provenance"] == PROVENANCE_OPENALEX
    return pub


def _apply_non_pubmed_work(
    db: AsyncSession, user_id: uuid.UUID, work: Mapping[str, Any],
    by_doi: dict[str, Publication], by_title: dict[str, list[Publication]],
    outcome: NonPubmedOutcome,
) -> None:
    """One work of ``apply_non_pubmed_works`` (Q13; B-D2); keeps both indexes current."""
    key = title_key(work["title"])
    row = by_doi.get(_doi_key(work["doi"]))
    if row is not None:
        if row.pmid is not None or row.provenance == PROVENANCE_MANUAL:
            outcome.skipped += 1
            return
        # A retitled work keeps its stored title when another row holds the new one.
        others = [r for r in by_title.get(key, []) if r is not row] if key else []
        fields = _NON_PUBMED_FIELDS[1:] if others else _NON_PUBMED_FIELDS
        if _refresh_non_pubmed(row, work, fields=fields):
            _note_refresh(outcome, work)
        if work.get("duplicate_of") and exclude_by_system(row, EXCLUDED_REASON_DUPLICATE_TITLE):
            outcome.duplicates_excluded.append(work["doi"])
        return
    if work.get("duplicate_of"):
        _insert_non_pubmed(db, user_id, work, by_doi, by_title, outcome, excluded=True)
        return
    holder = next((
        r for r in by_title.get(key, [])
        if same_paper_by_title(r.title, r.year, work["title"], work.get("year"))
    ), None) if key else None
    if holder is None:
        _insert_non_pubmed(db, user_id, work, by_doi, by_title, outcome, excluded=False)
    elif (holder.excluded_at is not None or holder.pmid is not None
            or holder.provenance == PROVENANCE_MANUAL):
        # A staff exclusion holds, PubMed has the paper, or a person kept it.
        outcome.skipped += 1
    elif (work.get("year") or 0) > (holder.year or 0):
        _insert_non_pubmed(db, user_id, work, by_doi, by_title, outcome, excluded=False)
        if exclude_by_system(holder, EXCLUDED_REASON_DUPLICATE_TITLE):
            outcome.duplicates_excluded.append(holder.doi or holder.title)
    else:
        _insert_non_pubmed(db, user_id, work, by_doi, by_title, outcome, excluded=True)


async def apply_non_pubmed_works(
    db: AsyncSession, user_id: uuid.UUID, works: Sequence[Mapping[str, Any]],
) -> NonPubmedOutcome:
    """Store a PubMed-thin PI's non-PubMed works (spec 2026-10-09 §5.3; the records of
    ``non_pubmed_works.collect_non_pubmed_works``) in the caller's transaction, under the
    ``lock_corpus`` the caller already holds.

    A work whose DOI a stored row carries refreshes that row when it is PMID-less and not
    ``manual`` (keeping its title if another row holds the new one) and is skipped
    otherwise. A work marked ``duplicate_of`` (``non_pubmed_works._one_per_title``) is stored
    excluded, or its in-use DOI row is excluded, reason ``duplicate_title``. A work that is
    the same paper by title (``same_paper_by_title``) as a stored row is skipped when that row
    is excluded, has a PMID or is ``manual``; otherwise the newer of the two is in use and the
    other excluded, and the stored row keeps its DOI. A short title never matches. Anything
    else is inserted with no PMID and ``doi_verified`` NULL. Never deletes. Flushes, never
    commits."""
    rows = (await db.execute(
        select(Publication).where(Publication.user_id == user_id)
        .order_by(Publication.created_at, Publication.id)
    )).scalars().all()
    by_doi: dict[str, Publication] = {}
    by_title: dict[str, list[Publication]] = {}
    for row in rows:
        if row.doi:
            by_doi.setdefault(_doi_key(row.doi), row)
        if (key := title_key(row.title)) is not None:
            by_title.setdefault(key, []).append(row)
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
