"""Per-PI publication-corpus repair
(docs/plans/2026-09-22-pi-corpus-attribution-remediation-plan.md §5, Task 3).

Preview by default; ``--apply`` is required for any write. Mirrors
``scripts/enqueue_enrichment.py``'s conventions (preview/--apply,
--orcid/--only scoping).

The evidence step is NOT optional: ``publications`` stores no ``pub_types``
and no author list, so every stored PMID in scope is refetched from PubMed
before any removal/addition decision is made (D8, plan Task 3). Unlike the retired sparse-data seeding script, which bulk-deleted a PI's corpus
before re-inserting, this one never bulk-deletes and never
removes a row without printing its evidence first.

Exclusion set (automatic, still gated on --apply; nothing is ever deleted):
  - every stored row but the first (by ``created_at``, then ``id``) that
    shares an identical PMID (``duplicate_pmid`` — observed, not enforced, until
    migration 0056 adds ``uq_publications_user_pmid``);
  - rows whose refetched record fails the CURRENT ``match_pi_author`` and lists
    authors, none of whom carries the PI's surname (``no_individual_author_match``);
  - rows whose refetched ``pub_types`` intersect ``EXCLUDED_TYPES`` with NO
    other type alongside (``excluded_type``); a row carrying a non-excluded
    type too (e.g. ``Comment`` + ``Journal Article``) is routed to review
    instead (``secondary_excluded_type``), never auto-removed (D4);
  - the older member of each same-paper pair among the survivors, by the pipeline's own
    rule (``corpus_additions.same_paper_by_title``: a long enough title and years close
    together; the newer, then the higher PMID, is kept) (``duplicate_title``).

Review set (``--review``, NEVER applied automatically):
  - rows whose only identity evidence is a bare single-letter forename/initial
    (``bare_initial_only``, D3 — a judgement call, not a mechanical one);
  - rows that fail ``match_pi_author`` on weaker evidence: no author list
    (``unverifiable_no_authors``), consortium-only (``consortium_only``), several authors
    matching the PI's name (``ambiguous_author``), an author list PubMed marks incomplete
    without the PI (``unverifiable_incomplete_list``), or the PI's surname present with a
    disagreeing forename (``forename_mismatch``);
  - rows with a secondary excluded type (``secondary_excluded_type``, D4);
  - resolved additions found without an ORCID anchor (``unanchored_addition``);
  - rows with no stored PMID at all (``no_pmid`` — nothing to refetch);
  - rows whose stored PMID PubMed no longer returns a record for
    (``unrefetchable`` — a retracted/merged/mis-keyed PMID is not evidence of
    mis-attribution, so it is never auto-removed on that basis alone).

Addition set: ``resolve_corpus``'s uncapped ``ranked`` list (year-DESC/PMID-DESC),
excluding PMIDs already stored, by the profile pipeline's own rule
(``select_corpus_additions``): only ORCID-anchored finds are stored, with their
provenance and a DOI reconciled against PubMed (``doi_verified``); records found by
OpenAlex or name+affiliation search alone go to review as ``unanchored_addition``.
Each PI is applied under the persona and corpus locks, and its persona is re-exported
after the commit (spec 2026-10-05 §4.3).

``--apply`` never deletes (Q4 of the 2026-10-15 publication-handling design). Each exclusion
sets ``excluded_at`` and ``excluded_reason = 'repair_script'`` through
``corpus_additions.exclude_by_system``; the row is kept, and staff Restore on the manager PI
page undoes it (the row then turns ``manual``). ``manual`` and excluded rows are never read,
and the count of them is printed. The matcher reads a row's ORCID anchor from its provenance
(stage s1 or s3), as the pipeline's gate reads a record's stages.

Usage (run via ``docker compose -f docker-compose.prod.yml run --rm --no-deps -T
blackbird-app``, never ``exec`` — the running container may still hold the
pre-fix matcher, plan Task 4):

    # Preview everyone:
    python scripts/repair_pi_corpus.py

    # Preview one PI, print the review report too:
    python scripts/repair_pi_corpus.py --orcid 0000-0000-0000-0000 --review

    # Apply only the automatic exclusions for two PIs (the scope keeps its name):
    python scripts/repair_pi_corpus.py --orcid 0000-... --orcid 0000-... \\
        --only removals --apply

    # Apply only the additions:
    python scripts/repair_pi_corpus.py --only additions --apply

    # The 0056 publications dedupe (duplicate_pmid removals only). Runs only
    # with the owner's go-ahead, BEFORE migration 0056 adds
    # ``uq_publications_user_pmid``; once the constraint exists duplicates
    # cannot occur and this finds nothing:
    python scripts/repair_pi_corpus.py --only duplicate-pmids --apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, or_, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from src.database import get_session_factory  # noqa: E402
from src.models import AgentRegistry, Publication, User  # noqa: E402
from src.models.publication import EXCLUDED_REASON_REPAIR_SCRIPT, PROVENANCE_MANUAL  # noqa: E402
from src.services import openalex_budget  # noqa: E402
from src.services.corpus import (  # noqa: E402
    EXCLUDED_TYPES,
    CorpusStageError,
    _match_pi_author_detail,
    _pi_name,
    _split_particle_splice,
    _surname_matches,
    match_pi_author,
    resolve_corpus,
)
from src.services.corpus_additions import (  # noqa: E402
    ANCHOR_STAGES,
    bounded_publication_fields,
    exclude_by_system,
    lock_corpus,
    provenance_for,
    reconcile_record_doi,
    same_paper_by_title,
    select_corpus_additions,
    title_key,
)
from src.services.job_queue import JobDeferred  # noqa: E402
from src.services.profile_publish import (  # noqa: E402
    lock_persona_writer,
    reexport_persona,
    write_persona_files,
)
from src.services.pubmed import fetch_pubmed_records  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("repair_pi_corpus")

REPORT_DIR = Path("docs/audits/2026-09-22-pi-corpus")


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredPublication:
    """The subset of a stored ``Publication`` row this script needs.

    A plain dataclass (not the ORM row itself) so the pure classification
    functions below are testable with fixtures and never touch a session.
    """

    id: str
    pmid: str | None
    title: str
    year: int | None
    provenance: str | None = None


@dataclass(frozen=True)
class RemovalCandidate:
    publication_id: str
    pmid: str | None
    reason: str
    title: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewCandidate:
    publication_id: str
    pmid: str | None
    reason: str
    title: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class PiRepairPlan:
    user_id: uuid.UUID
    orcid: str
    name: str
    agent_id: str | None
    removals: list[RemovalCandidate] = field(default_factory=list)
    review: list[ReviewCandidate] = field(default_factory=list)
    additions: list[dict[str, Any]] = field(default_factory=list)
    # pmid -> the ORCID-curated DOI from the resolve (CorpusResult.orcid_dois): the
    # candidate reconcile_record_doi checks against the PubMed record's DOI.
    orcid_dois: dict[str, str] = field(default_factory=dict)
    additions_error: str | None = None
    skipped: int = 0  # manual or excluded rows, never read


# ---------------------------------------------------------------------------
# Pure decision logic — no DB, no network. This is what the unit tests exercise.
# ---------------------------------------------------------------------------


def normalize_title(title: str | None) -> str:
    """Same normalization ``resolve_corpus`` uses for its own dedupe:
    lowercase, alphanumeric only."""
    return "".join(c for c in (title or "").lower() if c.isalnum())


def pmid_sort_key(pmid: str | None) -> int:
    try:
        return int(pmid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def classify_excluded_type(pub_types: Iterable[str]) -> str | None:
    """``None`` (clean), ``"excluded_type"`` (only excluded types present), or
    ``"secondary_excluded_type"`` (an excluded type alongside a real one, D4)."""
    types = {t.lower() for t in pub_types or []}
    hit = types & EXCLUDED_TYPES
    if not hit:
        return None
    return "excluded_type" if not (types - EXCLUDED_TYPES) else "secondary_excluded_type"


def partition_duplicate_pmids(
    stored: Sequence[StoredPublication],
) -> tuple[list[RemovalCandidate], list[StoredPublication]]:
    """Split out extra rows sharing an identical stored PMID (D8: the index
    is non-unique, so this is observed, not enforced). The first row seen per
    PMID is kept as canonical; later ones are automatic removals — they are
    the exact same paper stored twice, not a judgement call.

    "First seen" is the load order of ``load_stored_publications``:
    ``created_at``, then ``id``. Rows written in the same transaction share
    ``created_at`` (it is the transaction's ``now()``), so their ties fall back
    to ``id`` — deterministic, though not insertion order."""
    seen: dict[str, StoredPublication] = {}
    removals: list[RemovalCandidate] = []
    canonical: list[StoredPublication] = []
    for pub in stored:
        if pub.pmid is None:
            canonical.append(pub)
            continue
        rival = seen.get(pub.pmid)
        if rival is None:
            seen[pub.pmid] = pub
            canonical.append(pub)
        else:
            removals.append(
                RemovalCandidate(
                    publication_id=pub.id,
                    pmid=pub.pmid,
                    reason="duplicate_pmid",
                    title=pub.title,
                    evidence={"duplicate_of_publication_id": rival.id},
                )
            )
    return removals, canonical


def _record_has_surname(record: dict[str, Any], pi_name: str) -> bool:
    """Whether ANY valid listed author carries the PI's surname.

    Uses the service's own surname comparison (folding, joins, containment and
    particle-splice handling) so this agrees with `match_pi_author` about what a surname
    match is — the difference between the two is purely the forename leg, which is what
    makes "surname present, forename disagrees" a reviewable case rather than a removable
    one. The surname forms are the gate's own (``corpus._pi_name``): "Jane Doe, PhD" is a
    Doe, "Doe, Jane" a Doe, "John Smith Jr." a Smith; an ORCID-iD name has none. Entries
    PubMed marks ``ValidYN="N"`` are ignored, as the gate ignores them.
    """
    surnames = _pi_name(pi_name).surnames
    if not surnames:
        return False
    for author in record.get("authors") or []:
        if author.get("collective") or author.get("valid", True) is False:
            continue
        candidate = (author.get("last") or "").strip()
        if not candidate:
            continue
        if any(_surname_matches(candidate, s) for s in surnames):
            return True
        fore = (author.get("fore") or "").strip()
        splice = _split_particle_splice(fore) if fore else None
        if splice and any(_surname_matches(f"{splice[0]}{candidate}", s) for s in surnames):
            return True
    return False


def _row_is_anchored(pub: StoredPublication) -> bool:
    """The stored row's provenance names an ORCID anchor stage (s1 or s3): the matcher then
    judges it as the pipeline's gate judges an anchored record (R3.8)."""
    return bool(set((pub.provenance or "").split(",")) & ANCHOR_STAGES)


def classify_stored_publications(
    stored: Sequence[StoredPublication],
    refetched: dict[str, dict[str, Any]],
    pi_name: str,
    pi_orcid: str | None = None,
) -> tuple[list[RemovalCandidate], list[ReviewCandidate], list[tuple[StoredPublication, dict[str, Any]]]]:
    """Partition (already PMID-deduped) stored rows into removals, review
    items, and survivors — rows that pass identity + pub-type checks and
    proceed to the duplicate-title pass.

    ``pi_orcid`` is passed to the matcher so rows the pipeline stored through its
    ORCID-first pass (an author carrying the PI's ORCID, surname matching) classify
    the same way here; None means name-only matching. A row's ORCID anchor comes from
    its provenance (``_row_is_anchored``)."""
    removals: list[RemovalCandidate] = []
    review: list[ReviewCandidate] = []
    survivors: list[tuple[StoredPublication, dict[str, Any]]] = []

    for pub in stored:
        if pub.pmid is None:
            review.append(
                ReviewCandidate(
                    publication_id=pub.id,
                    pmid=None,
                    reason="no_pmid",
                    title=pub.title,
                    evidence={},
                )
            )
            continue
        record = refetched.get(pub.pmid)
        if record is None:
            review.append(
                ReviewCandidate(
                    publication_id=pub.id,
                    pmid=pub.pmid,
                    reason="unrefetchable",
                    title=pub.title,
                    evidence={},
                )
            )
            continue

        # An exclusion takes a row out of every reader until staff restore it, so auto-exclusion is reserved for
        # the ONE case where the record positively contradicts the
        # attribution: it lists authors, and none of them carries the PI's
        # surname. Everything weaker goes to review. The 2026-09-22 dry run
        # is why — running the coarse "kind != individual -> remove" rule
        # over production proposed deleting two correct rows:
        #
        #   * Ana Pombo / 37336950 — PubMed returns this record with an EMPTY
        #     AuthorList. No authors is absence of evidence, not evidence of
        #     absence, and Genome Architecture Mapping is Pombo's own method.
        #   * Ioannis Kevrekidis / 42215486 — the record says "Kevrekidis,
        #     Yannis". Yannis IS Ioannis; the forename simply does not start
        #     with the stored one. A forename disagreement is exactly the
        #     Daniel-vs-David-Liu judgement call a human has to make, in
        #     either direction.
        #
        # A consortium-only record is likewise kept for review: `resolve_corpus`
        # declines to ADD one (R1, not individual lab output), which is a very
        # different act from deleting one somebody already verified.
        authors = record.get("authors") or []
        anchored = _row_is_anchored(pub)
        kind, _ = match_pi_author(record, pi_name, pi_orcid, anchored=anchored)
        if kind != "individual":
            if not authors:
                reason = "unverifiable_no_authors"
            elif kind == "consortium":
                reason = "consortium_only"
            elif kind == "ambiguous":
                reason = "ambiguous_author"
            elif kind == "incomplete_list":
                reason = "unverifiable_incomplete_list"
            elif _record_has_surname(record, pi_name):
                reason = "forename_mismatch"
            else:
                reason = None  # positively not this PI — the one auto-removal
            evidence = {
                "authors": authors,
                "pub_types": record.get("pub_types") or [],
                "match_kind": kind,
            }
            if reason is not None:
                review.append(
                    ReviewCandidate(
                        publication_id=pub.id,
                        pmid=pub.pmid,
                        reason=reason,
                        title=pub.title,
                        evidence=evidence,
                    )
                )
            else:
                removals.append(
                    RemovalCandidate(
                        publication_id=pub.id,
                        pmid=pub.pmid,
                        reason="no_individual_author_match",
                        title=pub.title,
                        evidence=evidence,
                    )
                )
            continue

        type_hit = classify_excluded_type(record.get("pub_types") or [])
        if type_hit == "excluded_type":
            removals.append(
                RemovalCandidate(
                    publication_id=pub.id,
                    pmid=pub.pmid,
                    reason="excluded_type",
                    title=pub.title,
                    evidence={"pub_types": record.get("pub_types") or []},
                )
            )
            continue
        if type_hit == "secondary_excluded_type":
            review.append(
                ReviewCandidate(
                    publication_id=pub.id,
                    pmid=pub.pmid,
                    reason="secondary_excluded_type",
                    title=pub.title,
                    evidence={"pub_types": record.get("pub_types") or []},
                )
            )
            continue

        if _match_pi_author_detail(record, pi_name, pi_orcid, anchored=anchored)[2]:
            review.append(
                ReviewCandidate(
                    publication_id=pub.id,
                    pmid=pub.pmid,
                    reason="bare_initial_only",
                    title=pub.title,
                    evidence={"authors": record.get("authors") or []},
                )
            )
            # A judgement call, per the plan (D3) — the row is NOT auto-
            # removed, but it also does not proceed to duplicate-title
            # collapse as an anonymous survivor; it stays exactly as stored.
            continue

        survivors.append((pub, record))

    return removals, review, survivors


def find_duplicate_title_removals(
    survivors: Sequence[tuple[StoredPublication, dict[str, Any]]],
) -> tuple[list[RemovalCandidate], list[tuple[StoredPublication, dict[str, Any]]]]:
    """The pipeline's same-paper title rule (``corpus_additions.same_paper_by_title``: a
    title key of at least TITLE_KEY_MIN_CHARS characters, years at most SAME_PAPER_YEARS
    apart) over the survivor set. Survivors are walked newest first (the refetched year,
    else the stored one; then the higher PMID); one that is the same paper as an
    already-kept survivor is a ``duplicate_title`` exclusion. A short title such as
    "Introduction", or years further apart, never merges. The superseded-version rule of
    ``resolve_corpus`` is not applied here."""
    def year_of(item: tuple[StoredPublication, dict[str, Any]]) -> int | None:
        return item[1].get("year") or item[0].year

    def title_of(item: tuple[StoredPublication, dict[str, Any]]) -> str:
        return item[1].get("title") or item[0].title

    ordered = sorted(survivors, key=lambda item: (
        year_of(item) is None, -(year_of(item) or 0), -pmid_sort_key(item[0].pmid)))
    removals: list[RemovalCandidate] = []
    kept: list[tuple[StoredPublication, dict[str, Any]]] = []
    by_key: dict[str, list[tuple[StoredPublication, dict[str, Any]]]] = {}
    for item in ordered:
        key = title_key(title_of(item))
        keeper = next((k for k in by_key.get(key, []) if same_paper_by_title(
            title_of(k), year_of(k), title_of(item), year_of(item))), None) if key else None
        if keeper is None:
            kept.append(item)
            if key:
                by_key.setdefault(key, []).append(item)
            continue
        pub = item[0]
        removals.append(RemovalCandidate(
            publication_id=pub.id, pmid=pub.pmid, reason="duplicate_title", title=pub.title,
            evidence={"kept_publication_id": keeper[0].id, "kept_pmid": keeper[0].pmid},
        ))
    return removals, kept


def build_removal_and_review_plan(
    stored: Sequence[StoredPublication],
    refetched: dict[str, dict[str, Any]],
    pi_name: str,
    pi_orcid: str | None = None,
) -> tuple[list[RemovalCandidate], list[ReviewCandidate]]:
    """The whole pure pipeline: dedupe PMIDs, classify, collapse duplicate
    titles. Runs entirely on fixtures in tests; no DB, no network."""
    dup_pmid_removals, canonical = partition_duplicate_pmids(stored)
    id_removals, review, survivors = classify_stored_publications(
        canonical, refetched, pi_name, pi_orcid
    )
    title_removals, _kept = find_duplicate_title_removals(survivors)
    return dup_pmid_removals + id_removals + title_removals, review


# ---------------------------------------------------------------------------
# I/O layer
# ---------------------------------------------------------------------------


async def load_stored_publications(
    db: AsyncSession, user_id: uuid.UUID
) -> list[StoredPublication]:
    rows = (
        (
            await db.execute(
                select(Publication)
                .where(
                    Publication.user_id == user_id,
                    # Not the non-PubMed corpus (spec 2026-10-09 §5.7): a PMID-less row with a
                    # provenance was stored by a run on purpose, not left behind by PubMed.
                    or_(Publication.pmid.isnot(None), Publication.provenance.is_(None)),
                    # Never read a row a person kept or accepted, or an excluded row (Q4).
                    Publication.excluded_at.is_(None),
                    or_(Publication.provenance.is_(None),
                        Publication.provenance != PROVENANCE_MANUAL),
                )
                .order_by(Publication.created_at, Publication.id)
            )
        )
        .scalars()
        .all()
    )
    return [
        StoredPublication(
            id=str(p.id), pmid=p.pmid, title=p.title or "", year=p.year, provenance=p.provenance
        )
        for p in rows
    ]


async def count_untouchable_publications(db: AsyncSession, user_id: uuid.UUID) -> int:
    """The PI's rows the repair never reads: ``manual`` rows and excluded rows (Q4)."""
    return await db.scalar(
        select(func.count()).select_from(Publication).where(
            Publication.user_id == user_id,
            or_(Publication.excluded_at.isnot(None),
                Publication.provenance == PROVENANCE_MANUAL),
        )
    ) or 0


async def refetch_pmids(pmids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Refetch every given PMID from PubMed. Paced by ``fetch_pubmed_records``
    itself (per-process, D14) — callers must not run this concurrently across
    PIs, only sequentially."""
    if not pmids:
        return {}
    records = await fetch_pubmed_records(list(pmids))
    return {str(r["pmid"]): r for r in records if r.get("pmid")}


async def build_plan_for_pi(
    db: AsyncSession,
    user: User,
    agent_id: str | None,
    *,
    only: str | None,
) -> PiRepairPlan:
    plan = PiRepairPlan(
        user_id=user.id, orcid=user.orcid, name=user.name, agent_id=agent_id
    )
    stored = await load_stored_publications(db, user.id)
    plan.skipped = await count_untouchable_publications(db, user.id)
    # Read-only phase done: end the transaction so no connection sits idle in
    # transaction across the PubMed/OpenAlex/ORCID round trips below.
    await db.commit()

    if only == "duplicate-pmids":
        # The 0056 publications dedupe: the duplicate_pmid partition alone, no
        # network evidence needed (the rows are the same paper stored twice).
        plan.removals, _canonical = partition_duplicate_pmids(stored)
        return plan

    if only in (None, "removals"):
        pmids = sorted({p.pmid for p in stored if p.pmid})
        logger.info(
            "[%s] refetching %d stored PMID(s) for evidence", user.orcid, len(pmids)
        )
        refetched = await refetch_pmids(pmids)
        plan.removals, plan.review = build_removal_and_review_plan(
            stored, refetched, user.name, user.orcid
        )

    if only in (None, "additions"):
        try:
            with openalex_budget.bulk_requests():
                await openalex_budget.check_inline_corpus_budget()
                resolved = await resolve_corpus(user.orcid, user.name, user.institution)
        except (CorpusStageError, JobDeferred) as exc:
            plan.additions_error = str(exc)
            logger.error("[%s] resolve_corpus failed: %s", user.orcid, exc)
        else:
            stored_pmids = {p.pmid for p in stored if p.pmid}
            # getattr: tests pass a SimpleNamespace carrying only ``kept``.
            ranked = getattr(resolved, "ranked", None) or resolved.kept
            adds = select_corpus_additions(ranked, stored_pmids)
            plan.additions = adds.to_store
            plan.orcid_dois = dict(getattr(resolved, "orcid_dois", None) or {})
            plan.review.extend(
                ReviewCandidate(
                    publication_id="",
                    pmid=rec.get("pmid"),
                    reason="unanchored_addition",
                    title=rec.get("title") or "",
                    evidence={"stages": rec.get("stages") or []},
                )
                for rec in adds.review_only
            )

    return plan


async def _apply_plan(db: AsyncSession, plan: PiRepairPlan) -> bool:
    """One transaction per PI (a per-PI failure must not abort the run —
    callers catch around this).

    Explicit commit/rollback rather than ``async with db.begin()``: this
    session has already issued the SELECTs that built the plan, and an
    AsyncSession begins implicitly on first use, so ``begin()`` raises
    "A transaction is already begun on this Session" — measured 2026-09-22,
    when it failed for all 73 PIs and wrote nothing. Committing per PI keeps
    the same atomicity and the same failure isolation.

    The persona writer and corpus locks are taken first and held to the commit, and the
    removal rows are re-read by id under them: a row staff kept (``manual``) or excluded
    since planning is skipped. A removal is an exclusion with reason ``repair_script``
    (``exclude_by_system``); nothing is deleted, and staff Restore undoes it. The stored
    PMIDs are re-read under the locks: an addition that landed since planning is skipped
    rather than inserted twice.

    When the plan changed a row, the persona revision is recorded in the same
    transaction (``reexport_persona``, mechanism ``reexport``); the caller writes the
    file after the commit (``write_persona_files``, spec 2026-10-05 §4.3). Returns True
    when that revision was rendered (a file write is owed), False otherwise.
    """
    try:
        # Persona writer locks first (profile_publish lock order), then the corpus lock.
        await lock_persona_writer(db, plan.user_id)
        await lock_corpus(db, plan.user_id)
        stored_now = set(
            (
                await db.execute(
                    select(Publication.pmid).where(
                        Publication.user_id == plan.user_id,
                        Publication.pmid.isnot(None),
                    )
                )
            ).scalars()
        )
        changed = False
        if plan.removals:
            ids = [uuid.UUID(r.publication_id) for r in plan.removals]
            rows = (await db.execute(
                select(Publication).where(
                    Publication.user_id == plan.user_id, Publication.id.in_(ids),
                )
            )).scalars().all()
            for row in rows:
                if row.provenance == PROVENANCE_MANUAL or row.excluded_at is not None:
                    print(f"    skipped (now manual or excluded): pmid={row.pmid}")
                    continue
                changed = exclude_by_system(row, EXCLUDED_REASON_REPAIR_SCRIPT) or changed
        for rec in plan.additions:
            if rec.get("pmid") in stored_now:
                print(f"    already stored since planning: pmid={rec.get('pmid')}")
                continue
            doi, verified = reconcile_record_doi(rec, plan.orcid_dois)
            bounded = bounded_publication_fields(
                pmid=rec.get("pmid"), doi=doi, pmcid=rec.get("pmcid"), journal=rec.get("journal"),
            )
            if bounded["doi"] is None:
                verified = None
            db.add(
                Publication(
                    user_id=plan.user_id,
                    pmid=rec.get("pmid"),
                    pmcid=bounded["pmcid"],
                    doi=bounded["doi"],
                    doi_verified=verified,
                    title=rec.get("title") or "",
                    abstract=rec.get("abstract") or "",
                    journal=bounded["journal"],
                    year=rec.get("year"),
                    provenance=provenance_for(rec),
                )
            )
            changed = True
        rendered = None
        if changed:
            await db.flush()
            rendered = await reexport_persona(
                db, plan.user_id, mechanism="reexport",
                change_summary="Corpus repaired (repair_pi_corpus)",
            )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return rendered is not None


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _print_plan(plan: PiRepairPlan, *, show_review: bool) -> None:
    print(f"\n=== {plan.name} ({plan.orcid}) ===")
    print(f"  Skipped (manual or excluded, never touched): {plan.skipped}")
    if plan.removals:
        print(f"  Exclusions ({len(plan.removals)}):")
        for r in plan.removals:
            print(f"    - pmid={r.pmid} [{r.reason}] {r.title[:70]!r}")
            print(f"      evidence: {r.evidence}")
    else:
        print("  Exclusions: none")

    if show_review:
        if plan.review:
            print(f"  Review — judgement call, NOT auto-applied ({len(plan.review)}):")
            for r in plan.review:
                print(f"    - pmid={r.pmid} [{r.reason}] {r.title[:70]!r}")
                print(f"      evidence: {r.evidence}")
        else:
            print("  Review: none")
    else:
        print(f"  Review: {len(plan.review)} item(s) — pass --review to print them")

    if plan.additions_error:
        print(f"  Additions: FAILED to resolve corpus ({plan.additions_error})")
    elif plan.additions:
        print(f"  Additions ({len(plan.additions)}):")
        for rec in plan.additions[:20]:
            print(f"    - pmid={rec.get('pmid')} ({rec.get('year')}) {str(rec.get('title'))[:70]!r}")
        if len(plan.additions) > 20:
            print(f"    ... and {len(plan.additions) - 20} more")
    else:
        print("  Additions: none")


def _write_report(plan: PiRepairPlan, *, applied: bool) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    safe_orcid = re.sub(r"[^0-9A-Za-z-]", "_", plan.orcid)
    path = REPORT_DIR / f"repair_{safe_orcid}.md"
    lines = [
        f"# Corpus repair — {plan.name} ({plan.orcid})",
        "",
        f"Generated: {datetime.now(UTC).isoformat()}",
        f"Applied: {applied}",
        "",
        f"## Exclusions ({len(plan.removals)})",
        "",
    ]
    if plan.removals:
        lines.append("| PMID | Reason | Title | Evidence |")
        lines.append("|---|---|---|---|")
        for r in plan.removals:
            lines.append(f"| {r.pmid} | {r.reason} | {r.title[:80]} | {r.evidence} |")
    else:
        lines.append("None.")
    lines += ["", f"## Review — human judgement, never auto-applied ({len(plan.review)})", ""]
    if plan.review:
        lines.append("| PMID | Reason | Title | Evidence |")
        lines.append("|---|---|---|---|")
        for r in plan.review:
            lines.append(f"| {r.pmid} | {r.reason} | {r.title[:80]} | {r.evidence} |")
    else:
        lines.append("None.")
    lines += ["", f"## Additions ({len(plan.additions)})", ""]
    if plan.additions_error:
        lines.append(f"Corpus resolution FAILED: {plan.additions_error}")
    elif plan.additions:
        lines.append("| PMID | Year | Title |")
        lines.append("|---|---|---|")
        for rec in plan.additions:
            lines.append(f"| {rec.get('pmid')} | {rec.get('year')} | {str(rec.get('title'))[:100]} |")
    else:
        lines.append("None.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


async def _run(
    orcids: list[str], only: str | None, review: bool, apply: bool
) -> int:
    async with get_session_factory()() as db:
        q = select(User).where(User.user_role == "pi")
        if orcids:
            q = q.where(User.orcid.in_(orcids))
        users = (await db.execute(q)).scalars().all()
        if not users:
            print("No matching PI(s).")
            return 0

        agent_rows = (
            await db.execute(
                select(AgentRegistry.user_id, AgentRegistry.agent_id).where(
                    AgentRegistry.user_id.in_([u.id for u in users])
                )
            )
        ).all()
        agent_ids = {uid: aid for uid, aid in agent_rows}

        total_removals = total_review = total_additions = 0
        failures = 0

        for user in users:
            plan = await build_plan_for_pi(
                db, user, agent_ids.get(user.id), only=only
            )
            _print_plan(plan, show_review=review)
            report_path = _write_report(plan, applied=apply)
            print(f"  report: {report_path}")

            total_review += len(plan.review)
            if plan.additions_error:
                failures += 1  # A partial repair must not report complete resolution.

            if apply:
                try:
                    owes_write = await _apply_plan(db, plan)
                except Exception:
                    failures += 1
                    logger.exception(
                        "Failed to apply repair for %s (%s) — continuing",
                        plan.name, plan.orcid,
                    )
                else:
                    # Counted only on a successful commit: a run that wrote
                    # nothing must not print totals that look like it did.
                    total_removals += len(plan.removals)
                    total_additions += len(plan.additions)
                    if owes_write:
                        path = await write_persona_files(db, plan.user_id)
                        print(
                            f"  persona: {path.name}" if path is not None
                            else "  persona write FAILED (see the ERROR log)"
                        )
            else:
                total_removals += len(plan.removals)
                total_additions += len(plan.additions)

        print("\n=== Totals ===")
        print(f"  PIs processed: {len(users)}")
        verb = "written" if apply else "planned"
        print(f"  Exclusions {verb}: {total_removals}")
        print(f"  Review items (never auto-applied): {total_review}")
        print(f"  Additions {verb}: {total_additions}")
        if apply:
            print(f"  Per-PI apply failures: {failures}")
        else:
            print("  [dry run] pass --apply to write these changes")
        return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--orcid", action="append", default=[], help="Scope to this ORCID (repeatable)"
    )
    parser.add_argument(
        "--only", choices=["removals", "additions", "duplicate-pmids"], default=None
    )
    parser.add_argument(
        "--review", action="store_true", help="Also print the --review judgement-call report"
    )
    parser.add_argument("--apply", action="store_true", help="Write the changes (default: preview)")
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args.orcid, args.only, args.review, args.apply)))


if __name__ == "__main__":
    main()
