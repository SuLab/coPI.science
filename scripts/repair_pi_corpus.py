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

Removal set (automatic, still gated on --apply):
  - every stored row but the first (by ``created_at``, then ``id``) that
    shares an identical PMID (``duplicate_pmid`` — observed, not enforced, until
    migration 0056 adds ``uq_publications_user_pmid``);
  - rows whose refetched record fails the CURRENT ``match_pi_author`` and lists
    authors, none of whom carries the PI's surname (``no_individual_author_match``);
  - rows whose refetched ``pub_types`` intersect ``EXCLUDED_TYPES`` with NO
    other type alongside (``excluded_type``); a row carrying a non-excluded
    type too (e.g. ``Comment`` + ``Journal Article``) is routed to review
    instead (``secondary_excluded_type``), never auto-removed (D4);
  - the earlier member of each duplicate-title group among the survivors,
    using ``resolve_corpus``'s own rule: normalize the title, keep the higher
    year, tiebreak on the higher PMID (``duplicate_title``).

Review set (``--review``, NEVER applied automatically):
  - rows whose only identity evidence is a bare single-letter forename/initial
    (``bare_initial_only``, D3 — a judgement call, not a mechanical one);
  - rows that fail ``match_pi_author`` on weaker evidence: no author list
    (``unverifiable_no_authors``), consortium-only (``consortium_only``), or
    the PI's surname present with a disagreeing forename (``forename_mismatch``);
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

Usage (run via ``docker compose -f docker-compose.prod.yml run --rm --no-deps -T
blackbird-app``, never ``exec`` — the running container may still hold the
pre-fix matcher, plan Task 4):

    # Preview everyone:
    python scripts/repair_pi_corpus.py

    # Preview one PI, print the review report too:
    python scripts/repair_pi_corpus.py --orcid 0000-0000-0000-0000 --review

    # Apply only the automatic removals for two PIs:
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

from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from src.database import get_session_factory  # noqa: E402
from src.models import AgentRegistry, Publication, User  # noqa: E402
from src.services import openalex_budget  # noqa: E402
from src.services.corpus import (  # noqa: E402
    EXCLUDED_TYPES,
    CorpusStageError,
    _match_pi_author_detail,
    _split_particle_splice,
    _surname_matches,
    match_pi_author,
    resolve_corpus,
)
from src.services.corpus_additions import (  # noqa: E402
    lock_corpus,
    provenance_for,
    reconcile_record_doi,
    select_corpus_additions,
)
from src.services.job_queue import JobDeferred  # noqa: E402
from src.services.person_names import is_orcid_like, surname_candidates  # noqa: E402
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
    """Whether ANY listed author carries the PI's surname.

    Uses the service's own surname comparison (folding, compound and
    particle-splice handling) so this agrees with `match_pi_author` about
    what a surname match is — the difference between the two is purely the
    forename leg, which is what makes "surname present, forename disagrees"
    a reviewable case rather than a removable one. The surname forms are the shared
    parser's (``person_names.surname_candidates``), as the gate reads them: "Jane Doe,
    PhD" is a Doe, "John Smith Jr." a Smith; an ORCID-iD name has none.
    """
    if is_orcid_like(pi_name):
        return False
    parts = pi_name.strip().split()
    surnames = surname_candidates(pi_name) or ((parts[-1],) if parts else ())
    if not surnames:
        return False
    for author in record.get("authors") or []:
        if author.get("collective"):
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


def classify_stored_publications(
    stored: Sequence[StoredPublication],
    refetched: dict[str, dict[str, Any]],
    pi_name: str,
) -> tuple[list[RemovalCandidate], list[ReviewCandidate], list[tuple[StoredPublication, dict[str, Any]]]]:
    """Partition (already PMID-deduped) stored rows into removals, review
    items, and survivors — rows that pass identity + pub-type checks and
    proceed to the duplicate-title pass."""
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

        # Deleting a stored row is one-way, so auto-removal is reserved for
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
        kind, _ = match_pi_author(record, pi_name)
        if kind != "individual":
            if not authors:
                reason = "unverifiable_no_authors"
            elif kind == "consortium":
                reason = "consortium_only"
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

        if _match_pi_author_detail(record, pi_name)[2]:
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
    """``resolve_corpus``'s own dedupe rule, applied to the survivor set:
    group by normalized title, keep the later record (higher year, tiebreak
    higher PMID), and remove the rest of the group."""
    groups: dict[str, list[tuple[StoredPublication, dict[str, Any]]]] = {}
    for pub, record in survivors:
        title = record.get("title") or pub.title
        key = normalize_title(title) or f"pmid:{pub.pmid}"
        groups.setdefault(key, []).append((pub, record))

    removals: list[RemovalCandidate] = []
    kept: list[tuple[StoredPublication, dict[str, Any]]] = []
    for group in groups.values():
        if len(group) < 2:
            kept.append(group[0])
            continue
        winner = max(
            group,
            key=lambda item: (
                item[1].get("year") or item[0].year or 0,
                pmid_sort_key(item[0].pmid),
            ),
        )
        kept.append(winner)
        for pub, _record in group:
            if pub is winner[0]:
                continue
            removals.append(
                RemovalCandidate(
                    publication_id=pub.id,
                    pmid=pub.pmid,
                    reason="duplicate_title",
                    title=pub.title,
                    evidence={
                        "kept_publication_id": winner[0].id,
                        "kept_pmid": winner[0].pmid,
                    },
                )
            )
    return removals, kept


def build_removal_and_review_plan(
    stored: Sequence[StoredPublication],
    refetched: dict[str, dict[str, Any]],
    pi_name: str,
) -> tuple[list[RemovalCandidate], list[ReviewCandidate]]:
    """The whole pure pipeline: dedupe PMIDs, classify, collapse duplicate
    titles. Runs entirely on fixtures in tests; no DB, no network."""
    dup_pmid_removals, canonical = partition_duplicate_pmids(stored)
    id_removals, review, survivors = classify_stored_publications(
        canonical, refetched, pi_name
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
                .where(Publication.user_id == user_id)
                .order_by(Publication.created_at, Publication.id)
            )
        )
        .scalars()
        .all()
    )
    return [
        StoredPublication(id=str(p.id), pmid=p.pmid, title=p.title or "", year=p.year)
        for p in rows
    ]


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
            stored, refetched, user.name
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

    The corpus advisory lock is taken first and held to the commit, so a
    concurrent profile-pipeline run cannot interleave its publication writes.
    The stored PMIDs are re-read under that lock: an addition that landed since
    planning is skipped rather than inserted twice.

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
            await db.execute(delete(Publication).where(Publication.id.in_(ids)))
            changed = True
        for rec in plan.additions:
            if rec.get("pmid") in stored_now:
                print(f"    already stored since planning: pmid={rec.get('pmid')}")
                continue
            doi, verified = reconcile_record_doi(rec, plan.orcid_dois)
            db.add(
                Publication(
                    user_id=plan.user_id,
                    pmid=rec.get("pmid"),
                    pmcid=rec.get("pmcid"),
                    doi=doi,
                    doi_verified=verified,
                    title=rec.get("title") or "",
                    abstract=rec.get("abstract") or "",
                    journal=rec.get("journal"),
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
    if plan.removals:
        print(f"  Removals ({len(plan.removals)}):")
        for r in plan.removals:
            print(f"    - pmid={r.pmid} [{r.reason}] {r.title[:70]!r}")
            print(f"      evidence: {r.evidence}")
    else:
        print("  Removals: none")

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
        f"## Removals ({len(plan.removals)})",
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
        print(f"  Removals {verb}: {total_removals}")
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
