"""Intake (S0-S1): fetch slices, document identity, cross-source links and S1
(spec 2026-10-30 §6.1, §7.2, §12.4; D3, D17, D38, D67, D68, F10, K8).

**Identity** (D38). One work found in OpenAlex and PubMed is one ``landscape_documents`` row.
A record matches a stored row on any shared DOI, PMID or OpenAlex id (or, for other kinds, its
own source id); the row is re-keyed in place to the highest-precedence id (`canonical_id`).
S1 is recomputed on every write. The summary is PubMed's abstract when PubMed has one, else the
source's own (m-N); S2 and S3 re-run only when the summary text changes.

**Qualification precedence.** An OpenAlex record's §5.2 roles replace a PubMed-only
``pubmed_last_author`` basis, and a later PubMed record never replaces OpenAlex roles.

**Tombstones** (F10). Two stored rows found to be one work are merged (`merge_documents`); the
dropped row keeps ``merged_into`` and ``s1_status = "excluded:merged"``, so no S1-S3 selection
or lookup returns it again.

**Slice keys.** A fetch slice is named by ``<form>:<argument>``; `slice_fetcher` turns one into
its source call:

- ``month:YYYY-MM``: one calendar month (OpenAlex, PubMed ``[dp]``, NSF, DataCite, GEO);
- ``quarter:YYYY-Qn``: one ODP quarter;
- ``fy:YYYY``, or ``fy:YYYY@YYYY-MM`` for the monthly refresh: one RePORTER fiscal year;
- ``feed:<label>``, ``daily:<date>`` for JHTV: the whole RSS feed;
- ``daily:YYYY-MM-DD``: OpenAlex publication dates of the last 120 days, PubMed ``[edat]`` of
  that day and the day before;
- ``sweep:YYYY-MM-DD/YYYY-MM``: one month of the weekly OpenAlex full-window sweep;
- ``week:YYYY-MM-DD`` (a Monday): NSF, DataCite and GEO for the five weeks before it and the
  week itself, ODP for the quarter holding it.

A slice is due when no fetch of it has ended (``complete``, or ``incomplete`` after its
retries), so period-named keys give the D17 cadence without a separate schedule table.
"""
from __future__ import annotations

import asyncio
import calendar
import contextlib
import hashlib
import json
import logging
import random
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.landscaping.institutions import BALTIMORE_OPENALEX_IDS
from src.landscaping.labs import (
    lab_key,
    person_keys,
    resolve_lab_aliases,
    unify_grant_pi,
    unify_name_key,
)
from src.landscaping.qualify import s1_status
from src.landscaping.sources import (
    DocumentRecord,
    SliceResult,
    datacite,
    geo,
    jhtv,
    nsf,
    odp_patents,
    openalex_works,
    pubmed_works,
    reporter,
)
from src.models import AppSetting
from src.models.landscape import (
    LandscapeDocument,
    LandscapeDocumentLink,
    LandscapeEpoch,
    LandscapeExtraction,
    LandscapeFetch,
    LandscapeScreen,
    LandscapeSource,
)
from src.services import nih_reporter, openalex, pubmed
from src.services.identifiers import normalize_doi, normalize_pmid
from src.services.job_queue import job_error_text

logger = logging.getLogger(__name__)

#: Every discovery source (spec §6.1), one ``landscape_sources`` row each.
SOURCES = ("openalex", "pubmed", "reporter", "nsf", "odp", "jhtv", "datacite", "geo")
#: Sources created disabled: JHTV waits until a person has read its terms of use (§15).
_DISABLED_AT_START = frozenset({"jhtv"})
#: Sources whose backfill runs only in NCBI's off-peak hours (K8).
NCBI_SOURCES = frozenset({"pubmed", "geo"})
#: Received ÷ expected below this marks a slice incomplete (§12.4).
COMPLETE_RATIO = 0.99
#: Tries per slice; an incomplete or failed try is retried after the matching delay.
FETCH_ATTEMPTS = 3
#: Seconds to wait before the second and third try.
RETRY_DELAYS: tuple[float, ...] = (60.0, 300.0)
#: The daily OpenAlex pull reaches back this far by publication date (§6.1).
DAILY_LOOKBACK_DAYS = 120
#: Weekly NSF, DataCite and GEO pulls reach back this far, so a late-indexed record is not
#: missed between two weeks.
WEEKLY_LOOKBACK_DAYS = 35
#: A failed slice is not due again until this long after it started.
FAILED_RETRY_AFTER = timedelta(hours=1)
#: A slice still ``running`` this long after it started is taken to have died with its worker.
RUNNING_STALE_AFTER = timedelta(hours=6)
#: RePORTER grant → PMID links are asked again after this long (§6.1).
GRANT_LINKS_REFRESH = timedelta(days=7)
#: A preprint and an article with the same title are one work only within this span (§6.1).
PREPRINT_MATCH_DAYS = 730
#: Upserts per committed chunk of a slice (§12.2: each unit commits on its own).
_STORE_CHUNK = 100

_WORK_KINDS = ("work", "preprint")
_MONTH_SOURCES = ("openalex", "pubmed", "nsf", "datacite", "geo")
_WEEKLY_SOURCES = ("nsf", "odp", "datacite", "geo")
_CANONICAL_ORDER = ("doi", "pmid", "openalex")
_OTHER_ORDER = (
    "core_project", "appl_id", "nsf_award", "patent_application", "jhtv_case",
    "datacite_doi", "geo_accession", "pmcid",
)
_COLUMN_IDS = {"doi": "doi", "pmid": "pmid", "pmcid": "pmcid", "openalex": "openalex_id"}
_EASTERN = ZoneInfo("America/New_York")


@dataclass
class FetchOutcome:
    """How one slice ended: ``complete``, ``incomplete`` (after its retries) or ``failed``."""

    source: str
    slice_key: str
    status: str
    expected: int | None
    received: int | None
    stored: int
    attempts: int
    fetch_id: uuid.UUID


# --- identity --------------------------------------------------------------------------------


def _clean_ids(ids: dict[str, str]) -> dict[str, str]:
    """Identifiers with blanks dropped and the DOI lowercased (DOIs are case-insensitive)."""
    out = {k: str(v).strip() for k, v in (ids or {}).items() if v is not None and str(v).strip()}
    if "doi" in out:
        out["doi"] = out["doi"].lower()
    return out


def canonical_id(ids: dict[str, str]) -> str:
    """``<scheme>:<value>`` of the highest-precedence identifier: DOI > PMID > OpenAlex id
    (D38), then the source ids (core project, application, award, patent application, JHTV
    case, dataset DOI, GEO accession, PMCID), then any other scheme in name order. RAISES
    ValueError when `ids` holds nothing."""
    clean = _clean_ids(ids)
    for scheme in (*_CANONICAL_ORDER, *_OTHER_ORDER, *sorted(clean)):
        if scheme in clean:
            return f"{scheme}:{clean[scheme]}"[:300]
    raise ValueError("record has no identifier")


def _precedence(doc: LandscapeDocument) -> tuple[int, datetime]:
    scheme = doc.canonical_external_id.split(":", 1)[0]
    rank = _CANONICAL_ORDER.index(scheme) if scheme in _CANONICAL_ORDER else len(_CANONICAL_ORDER)
    return rank, doc.created_at or datetime.max.replace(tzinfo=UTC)


def _row_ids(doc: LandscapeDocument) -> dict[str, str]:
    ids = {k: str(v) for k, v in (doc.external_ids or {}).items() if v}
    for scheme, column in _COLUMN_IDS.items():
        if value := getattr(doc, column):
            ids[scheme] = value
    return ids


def summary_of(rec: DocumentRecord, pubmed_abstract: str | None) -> str | None:
    """The text S2 and S3 read (m-N): PubMed's abstract when there is one, else the record's
    own summary (OpenAlex's reconstructed abstract, a grant or patent abstract, …)."""
    if pubmed_abstract and pubmed_abstract.strip():
        return pubmed_abstract
    return rec.summary if rec.summary and rec.summary.strip() else None


def _hash(text: str | None) -> str | None:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None


def _norm_title(title: str | None) -> str:
    """The exact-title form compared across records: case-folded, whitespace collapsed, no
    trailing full stop. SQL compares ``rtrim(lower(title), '. ')`` against it, so the stored
    title must have its whitespace collapsed already (`_apply_fields` does that)."""
    return " ".join((title or "").split()).lower().rstrip(". ")


def _title_expr():
    return func.rtrim(func.lower(LandscapeDocument.title), ". ")


def _jsonable(value):
    """`value` with dates and other non-JSON leaves as strings, for a JSONB column."""
    return json.loads(json.dumps(value, default=str))


def _from_openalex(rec: DocumentRecord) -> bool:
    return rec.kind in _WORK_KINDS and "openalex" in rec.ids


def _qualified_by(doc: LandscapeDocument) -> str | None:
    return (doc.details or {}).get("qualified_by")


def _record_from_row(doc: LandscapeDocument) -> DocumentRecord:
    """The stored row as a record, for recomputing S1 after a merge of sources."""
    details = doc.details or {}
    return DocumentRecord(
        kind=doc.kind, ids=_row_ids(doc), title=doc.title or "", summary=doc.summary_text,
        published=doc.published_on, doc_type=doc.document_type or "", language=doc.language,
        license=doc.license, authorships=doc.qualifying_authorships, role_basis=doc.role_basis,
        raw_qualifier=details.get("raw_qualifier") or {}, summary_source=doc.summary_source,
        retracted=bool(doc.is_retracted), senior_names=details.get("senior_names") or [],
        related_ids=details.get("related_ids") or {}, extra=details.get("extra") or {},
    )


def _refresh_s1(doc: LandscapeDocument) -> None:
    if doc.merged_into is not None:
        doc.s1_status = "excluded:merged"
        return
    doc.s1_status = s1_status(
        _record_from_row(doc), today=datetime.now(UTC).date(),
        window_days=get_settings().landscape_window_days,
    )


async def _live(db: AsyncSession, doc: LandscapeDocument) -> LandscapeDocument:
    """The row a tombstone was merged into, followed to the end of the chain."""
    for _ in range(10):
        if doc.merged_into is None:
            return doc
        doc = await db.get(LandscapeDocument, doc.merged_into)
    return doc


async def _matching_rows(
    db: AsyncSession, kind: str, ids: dict[str, str],
) -> list[LandscapeDocument]:
    """Live rows sharing any identifier with `ids`, tombstones followed, each once."""
    D = LandscapeDocument
    conds = [and_(D.kind == kind, D.canonical_external_id == canonical_id(ids))]
    for scheme in ("doi", "pmid", "openalex"):
        if scheme in ids:
            conds.append(getattr(D, _COLUMN_IDS[scheme]) == ids[scheme])
    if kind not in _WORK_KINDS:
        for scheme, value in ids.items():
            if scheme not in _COLUMN_IDS:
                conds.append(and_(D.kind == kind, D.external_ids[scheme].astext == value))
    rows = (await db.execute(select(D).where(or_(*conds)))).scalars().all()
    live: dict[uuid.UUID, LandscapeDocument] = {}
    for row in rows:
        target = await _live(db, row)
        live[target.id] = target
    return list(live.values())


# --- applying a record to a row ----------------------------------------------------------------


def _apply_ids(doc: LandscapeDocument, ids: dict[str, str]) -> None:
    """Fill identifiers the row lacks; a stored identifier is never overwritten."""
    for scheme, column in _COLUMN_IDS.items():
        if scheme in ids and not getattr(doc, column):
            setattr(doc, column, ids[scheme][: 255 if column == "doi" else 60])
    others = {k: v for k, v in ids.items() if k not in _COLUMN_IDS}
    if others:
        doc.external_ids = {**others, **(doc.external_ids or {})}


def _merged_type(old: str | None, new: str | None) -> str | None:
    """The more specific of two document types: a PubMed letter or comment is not overruled
    by OpenAlex's generic ``article``."""
    if old and old != "article" and (not new or new == "article"):
        return old
    return new or old


def _apply_fields(doc: LandscapeDocument, rec: DocumentRecord, *, authoritative: bool) -> None:
    """Descriptive fields. An `authoritative` record replaces them; a PubMed record for a
    row OpenAlex has already described fills only what is missing. The document type keeps
    the more specific value either way, and a retraction from either source stands."""
    def take(column: str, value) -> None:
        if value is not None and value != "" and (authoritative or getattr(doc, column) is None):
            setattr(doc, column, value)

    if authoritative:
        doc.kind = rec.kind
    take("title", " ".join(rec.title.split()) if rec.title else None)
    take("published_on", rec.published)
    take("language", (rec.language or "")[:10] or None)
    take("license", (rec.license or "")[:60] or None)
    doc.document_type = (_merged_type(doc.document_type, rec.doc_type) or "")[:40] or None
    doc.is_retracted = bool(doc.is_retracted) or rec.retracted
    open_access = rec.extra.get("open_access")
    if isinstance(open_access, dict):
        doc.oa_flags = _jsonable(open_access)
        doc.is_oa = open_access.get("is_oa")
    period = {k: rec.extra.get(k) for k in ("project_start", "project_end", "fiscal_year")
              if rec.extra.get(k) is not None}
    if period:
        doc.dates = _jsonable({**(doc.dates or {}), **period})
    details = dict(doc.details or {})
    details["extra"] = _jsonable({**(details.get("extra") or {}), **rec.extra})
    if rec.related_ids:
        details["related_ids"] = _jsonable(rec.related_ids)
    doc.details = details


def _apply_summary(doc: LandscapeDocument, rec: DocumentRecord) -> None:
    """Summary precedence (m-N): a PubMed abstract, stored or arriving, wins."""
    if rec.summary_source == "pubmed" and rec.summary:
        pubmed_abstract = rec.summary
    elif doc.summary_source == "pubmed":
        pubmed_abstract = doc.summary_text
    else:
        pubmed_abstract = None
    text = summary_of(rec, pubmed_abstract)
    if text is None:
        return
    doc.summary_text = text
    doc.summary_source = "pubmed" if pubmed_abstract else rec.summary_source
    doc.summary_hash = _hash(text)


def _institution(rec: DocumentRecord) -> str | None:
    """The Baltimore institution a record qualified through, for counts by institution."""
    rq = rec.raw_qualifier
    for inst in rq.get("institution_ids") or []:
        if inst in BALTIMORE_OPENALEX_IDS:
            return inst
    for key in ("affiliation", "org_name", "awardee"):
        if rq.get(key):
            return str(rq[key])[:100]
    names = list(rq.get("assignees") or []) + list(rq.get("applicants") or [])
    return str(names[0])[:100] if names else None


def _qualifying_authorships(rec: DocumentRecord) -> list | None:
    if _from_openalex(rec):
        return _jsonable(rec.authorships) if rec.authorships else None
    last = rec.raw_qualifier.get("last_author")
    return _jsonable([last]) if rec.role_basis and last else None


async def _resolve_lab(db: AsyncSession, rec: DocumentRecord) -> str | None:
    """The canonical lab key of a qualifying record (§5.2), lab rows written; the raw key of
    a record that does not qualify, with no lab row (labs are Baltimore labs only)."""
    if not rec.role_basis or not rec.senior_names:
        return lab_key(rec)
    person = rec.senior_names[0]
    keys = person_keys(person)
    id_keys = [k for k in keys if not k.startswith("name:")]
    name = person.get("name") or None
    if id_keys:
        return await resolve_lab_aliases(db, id_keys, display_name=name)
    if not keys:
        return None
    if rec.kind in ("patent", "jhtv"):
        return await unify_name_key(db, keys[0])
    return await resolve_lab_aliases(db, keys, display_name=name)


async def _apply_qualification(
    db: AsyncSession, doc: LandscapeDocument, rec: DocumentRecord,
) -> None:
    """The §5.2 qualification and lab key, unless the row already holds OpenAlex roles and
    this record is not OpenAlex's (D38)."""
    if not _from_openalex(rec) and _qualified_by(doc) == "openalex":
        return
    doc.role_basis = rec.role_basis
    doc.qualifying_authorships = _qualifying_authorships(rec) if rec.role_basis else None
    doc.qualifying_institution = _institution(rec) if rec.role_basis else None
    key = await _resolve_lab(db, rec)
    doc.lab_key = key[:200] if key else None
    details = dict(doc.details or {})
    details["qualified_by"] = (
        "openalex" if _from_openalex(rec) else (rec.summary_source or rec.kind)
    )
    details["raw_qualifier"] = _jsonable(rec.raw_qualifier)
    details["senior_names"] = _jsonable(rec.senior_names)
    doc.details = details


async def _rerun_judgments(db: AsyncSession, document_id: uuid.UUID) -> None:
    """Return the document's S2 row and summary-based S3 row to ``pending`` in every
    non-retired epoch (D38): the active one and any candidate, so a candidate does not keep
    a judgment of the old text. Clearing ``batch_item_id`` makes any in-flight batch result
    for the old text fail its compare-and-set (spec §7.1 step 5)."""
    epochs = select(LandscapeEpoch.id).where(LandscapeEpoch.status != "retired")
    stage_reset = dict(
        status="pending", batch_item_id=None, attempts=0, truncated_once=False,
        fallback_from=None, served_by_model=None, refusal_category=None, error=None,
        summary_hash=None, updated_at=func.now(),
    )
    await db.execute(
        update(LandscapeScreen)
        .where(LandscapeScreen.document_id == document_id, LandscapeScreen.epoch_id.in_(epochs))
        .values(**stage_reset, decision=None, kinds=None, mentions=None, reason=None)
    )
    await db.execute(
        update(LandscapeExtraction)
        .where(
            LandscapeExtraction.document_id == document_id,
            LandscapeExtraction.basis == "summary",
            LandscapeExtraction.epoch_id.in_(epochs),
        )
        .values(**stage_reset, raw=None, quotes_total=None, quotes_verified=None)
    )


async def _link(db: AsyncSession, doc_a: uuid.UUID, doc_b: uuid.UUID, kind: str) -> bool:
    """Insert one document link; False when it already exists or would link a row to itself."""
    if doc_a == doc_b:
        return False
    inserted = (await db.execute(
        pg_insert(LandscapeDocumentLink)
        .values(id=uuid.uuid4(), doc_a=doc_a, doc_b=doc_b, link_kind=kind)
        .on_conflict_do_nothing(index_elements=["doc_a", "doc_b", "link_kind"])
        .returning(LandscapeDocumentLink.id)
    )).scalar_one_or_none()
    return inserted is not None


async def _link_preprint(db: AsyncSession, doc: LandscapeDocument) -> None:
    """Link a preprint and its published article by exact title within two years (§6.1);
    ``doc_a`` is the preprint, ``doc_b`` the article."""
    if doc.kind not in _WORK_KINDS or not doc.title or doc.published_on is None:
        return
    D = LandscapeDocument
    other_kind = "work" if doc.kind == "preprint" else "preprint"
    span = timedelta(days=PREPRINT_MATCH_DAYS)
    others = (await db.execute(
        select(D.id).where(
            D.kind == other_kind,
            D.merged_into.is_(None),
            _title_expr() == _norm_title(doc.title),
            D.published_on.between(doc.published_on - span, doc.published_on + span),
        )
    )).scalars().all()
    for other in others:
        preprint, article = (doc.id, other) if doc.kind == "preprint" else (other, doc.id)
        await _link(db, preprint, article, "preprint_of")


async def upsert_document(db: AsyncSession, rec: DocumentRecord) -> uuid.UUID:
    """Store one record and return its document id (spec §6.1 cross-source linking, D38).

    It matches stored rows by any shared identifier, following a tombstone to its kept row.
    Two or more matching rows are merged first (`merge_documents`, the highest-precedence
    one kept). The row is then re-keyed in place to `canonical_id` of every identifier it
    now holds. S1 is recomputed. S2 and S3 re-run (every non-retired epoch) only when the
    summary text changed. Flushes; the caller commits. RAISES ValueError when the record
    has no identifier."""
    ids = _clean_ids(rec.ids)
    cid = canonical_id(ids)
    matched = await _matching_rows(db, rec.kind, ids)
    if matched:
        doc = min(matched, key=_precedence)
        for other in matched:
            if other.id != doc.id:
                await merge_documents(db, doc.id, other.id)
        # A PubMed record fills gaps in a row OpenAlex has described; any other record
        # describes its row afresh.
        authoritative = _from_openalex(rec) or not doc.openalex_id
    else:
        doc = LandscapeDocument(
            id=uuid.uuid4(), kind=rec.kind, canonical_external_id=cid,
            s1_status="excluded:not_qualified",
        )
        db.add(doc)
        authoritative = True
    old_hash, old_title, old_kind = doc.summary_hash, doc.title, doc.kind
    _apply_ids(doc, ids)
    _apply_fields(doc, rec, authoritative=authoritative)
    _apply_summary(doc, rec)
    await _apply_qualification(db, doc, rec)
    new_cid = canonical_id(_row_ids(doc))
    if new_cid != doc.canonical_external_id:
        doc.canonical_external_id = new_cid
    _refresh_s1(doc)
    await db.flush()
    if matched and doc.summary_hash != old_hash:
        await _rerun_judgments(db, doc.id)
    if not matched or doc.title != old_title or doc.kind != old_kind:
        await _link_preprint(db, doc)
    return doc.id


async def _repoint_links(db: AsyncSession, drop_id: uuid.UUID, keep_id: uuid.UUID) -> None:
    """Move the tombstone's document links to the kept row; a link that would duplicate one
    the kept row has, or join the row to itself, is deleted."""
    L = LandscapeDocumentLink
    links = (await db.execute(
        select(L).where(or_(L.doc_a == drop_id, L.doc_b == drop_id))
    )).scalars().all()
    for link in links:
        a = keep_id if link.doc_a == drop_id else link.doc_a
        b = keep_id if link.doc_b == drop_id else link.doc_b
        duplicate = (await db.execute(
            select(L.id).where(L.doc_a == a, L.doc_b == b, L.link_kind == link.link_kind)
        )).scalar_one_or_none()
        if a == b or duplicate is not None:
            await db.execute(delete(L).where(L.id == link.id))
        else:
            link.doc_a, link.doc_b = a, b
            await db.flush()


def _take_qualification(keep: LandscapeDocument, drop: LandscapeDocument) -> bool:
    if drop.role_basis is None:
        return False
    if keep.role_basis is None:
        return True
    return _qualified_by(drop) == "openalex" and _qualified_by(keep) != "openalex"


def _merge_fields(keep: LandscapeDocument, drop: LandscapeDocument) -> None:
    """Identifiers, summary (m-N precedence) and qualification the kept row takes over."""
    for column in _COLUMN_IDS.values():
        if not getattr(keep, column) and getattr(drop, column):
            setattr(keep, column, getattr(drop, column))
    if drop.external_ids:
        keep.external_ids = {**drop.external_ids, **(keep.external_ids or {})}
    if drop.summary_text and (
        keep.summary_text is None
        or (drop.summary_source == "pubmed" and keep.summary_source != "pubmed")
    ):
        keep.summary_text, keep.summary_source = drop.summary_text, drop.summary_source
        keep.summary_hash = _hash(drop.summary_text)
    if _take_qualification(keep, drop):
        keep.role_basis = drop.role_basis
        keep.qualifying_authorships = drop.qualifying_authorships
        keep.qualifying_institution = drop.qualifying_institution
        keep.lab_key = drop.lab_key
        details = dict(keep.details or {})
        for key in ("qualified_by", "raw_qualifier", "senior_names"):
            if key in (drop.details or {}):
                details[key] = drop.details[key]
        keep.details = details
    keep.is_retracted = bool(keep.is_retracted) or bool(drop.is_retracted)
    keep.document_type = _merged_type(keep.document_type, drop.document_type)
    for column in ("title", "published_on", "language", "license"):
        if getattr(keep, column) is None and getattr(drop, column) is not None:
            setattr(keep, column, getattr(drop, column))


async def merge_documents(db: AsyncSession, keep_id: uuid.UUID, drop_id: uuid.UUID) -> None:
    """Make `drop_id` a tombstone of `keep_id`: two stored rows found to be one work (spec
    §6.1, F10).

    The dropped row gets ``merged_into = keep_id`` and ``s1_status = "excluded:merged"``.
    Its own S2 and S3 rows stay attached to it as history and are not re-pointed. Its
    ``landscape_document_links`` move to the kept row; later epochs' idea evidence reaches
    the kept row on its own, since only the kept row is selected for S2-S3 again. The kept
    row takes the identifiers it lacks, the preferred summary and the stronger
    qualification. S1 is recomputed, and its S2/S3 re-run only when its summary was the one
    replaced. Flushes; the caller commits. A no-op when already merged or the same row."""
    if keep_id == drop_id:
        return
    keep = await db.get(LandscapeDocument, keep_id)
    drop = await db.get(LandscapeDocument, drop_id)
    if keep is None or drop is None or drop.merged_into == keep_id:
        return
    old_hash = keep.summary_hash
    _merge_fields(keep, drop)
    drop.merged_into = keep.id
    drop.s1_status = "excluded:merged"
    new_cid = canonical_id(_row_ids(keep))
    if keep.kind == drop.kind and new_cid == drop.canonical_external_id:
        # The tombstone gives up its key so the kept row can hold it.
        drop.canonical_external_id = f"merged:{drop.id}"
        await db.flush()
    keep.canonical_external_id = new_cid
    _refresh_s1(keep)
    await db.flush()
    await _repoint_links(db, drop.id, keep.id)
    if keep.summary_hash != old_hash:
        await _rerun_judgments(db, keep.id)


async def find_document(
    db: AsyncSession, *, doi: str | None = None, pmid: str | None = None,
    title: str | None = None,
) -> uuid.UUID | None:
    """The stored document a DOI, else a PMID, else an exact normalized title names (D67).

    Never a tombstone or a dataset. Among a work and its preprint the work is returned; a
    title that matches more than one candidate resolves to nothing."""
    D = LandscapeDocument
    base = (D.merged_into.is_(None), D.kind != "dataset")
    order = ((D.kind != "work"), D.created_at)
    if doi and (norm := normalize_doi(doi)):
        found = (await db.execute(
            select(D.id).where(D.doi == norm.lower(), *base).order_by(*order).limit(1)
        )).scalar_one_or_none()
        if found is not None:
            return found
    if pmid and (norm_pmid := normalize_pmid(pmid)):
        found = (await db.execute(
            select(D.id).where(D.pmid == norm_pmid, *base).order_by(*order).limit(1)
        )).scalar_one_or_none()
        if found is not None:
            return found
    if title and _norm_title(title):
        rows = (await db.execute(
            select(D.id, D.kind).where(_title_expr() == _norm_title(title), *base)
        )).all()
        works = [r for r in rows if r.kind == "work"]
        pick = works or rows
        if len(pick) == 1:
            return pick[0].id
    return None


# --- fetching ----------------------------------------------------------------------------------


def _ratio(expected: int | None, received: int | None) -> float:
    if not expected:
        return 1.0
    return (received or 0) / expected


async def _store_rows(db_factory, rows: list[DocumentRecord]) -> int:
    """Upsert a slice's records, committing every `_STORE_CHUNK`; a record with no
    identifier is logged and skipped."""
    stored = 0
    for start in range(0, len(rows), _STORE_CHUNK):
        async with db_factory() as db:
            for rec in rows[start:start + _STORE_CHUNK]:
                if not _clean_ids(rec.ids):
                    logger.warning("landscaping intake: %s record without an identifier", rec.kind)
                    continue
                await upsert_document(db, rec)
                stored += 1
            await db.commit()
    return stored


async def _update_fetch(db_factory, fetch_id: uuid.UUID, **values) -> None:
    async with db_factory() as db:
        await db.execute(
            update(LandscapeFetch).where(LandscapeFetch.id == fetch_id).values(**values)
        )
        await db.commit()


async def run_slice(
    db_factory, source: str, slice_key: str, fetch: Callable[[], Awaitable[SliceResult]],
) -> FetchOutcome:
    """Fetch one slice, store its records and write its completeness record (spec §12.4).

    A ``landscape_fetches`` row is committed first (``running``). Each try's records are
    upserted. A try below `COMPLETE_RATIO` received ÷ expected, or one that raised, is
    retried after `RETRY_DELAYS`, up to `FETCH_ATTEMPTS` tries. The row then ends
    ``complete``, ``incomplete`` (shown on the pipeline page; nothing downstream waits) or
    ``failed`` with the error through ``job_error_text``. The source's last success and
    completeness ratio are updated unless the slice failed."""
    async with db_factory() as db:
        row = LandscapeFetch(
            id=uuid.uuid4(), source=source, slice_key=slice_key,
            started_at=datetime.now(UTC), status="running", attempts=0,
        )
        db.add(row)
        await db.commit()
        fetch_id = row.id
    status, expected, received, error, stored, attempt = "failed", None, None, None, 0, 0
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            result = await fetch()
        except Exception as exc:  # any source failure is recorded on the slice
            status, error = "failed", job_error_text(exc)
            logger.warning("landscaping fetch %s %s try %d failed: %s",
                           source, slice_key, attempt, error)
        else:
            expected, received = result.expected, result.received
            stored += await _store_rows(db_factory, result.rows)
            complete = _ratio(expected, received) >= COMPLETE_RATIO
            status = "complete" if complete else "incomplete"
            error = None if complete else f"received {received} of {expected}"
        await _update_fetch(db_factory, fetch_id, attempts=attempt, expected_count=expected,
                            received_count=received, error=error)
        if status == "complete":
            break
        if attempt < FETCH_ATTEMPTS:
            await asyncio.sleep(RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)])
    await _update_fetch(db_factory, fetch_id, status=status, finished_at=datetime.now(UTC))
    if status != "failed":
        async with db_factory() as db:
            await db.execute(
                update(LandscapeSource).where(LandscapeSource.source == source).values(
                    last_success_at=datetime.now(UTC),
                    last_completeness_ratio=_ratio(expected, received),
                    updated_at=func.now(),
                )
            )
            await db.commit()
    return FetchOutcome(source, slice_key, status, expected, received, stored, attempt, fetch_id)


def _month_bounds(arg: str) -> tuple[date, date]:
    year, month = (int(x) for x in arg.split("-")[:2])
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _range_fetch(source: str, start: date, end: date) -> Callable[[], Awaitable[SliceResult]]:
    if source == "openalex":
        return lambda: openalex_works.fetch_slice(start, end)
    if source == "nsf":
        return lambda: nsf.fetch_nsf(start, end)
    if source == "datacite":
        return lambda: datacite.fetch_datacite(start, end)
    if source == "geo":
        return lambda: geo.fetch_geo(start, end)
    raise ValueError(f"no date-range fetch for source {source!r}")


def _quarter_of(day: date) -> tuple[int, int]:
    return day.year, (day.month - 1) // 3 + 1


def slice_fetcher(source: str, slice_key: str) -> Callable[[], Awaitable[SliceResult]]:
    """The source call behind one ``(source, slice_key)`` that `schedule_due` or
    `pilot_slices` returns (the key forms are in the module docstring). RAISES ValueError
    for a key the source does not take."""
    form, _, arg = slice_key.partition(":")
    if source == "jhtv":
        return jhtv.fetch_rss
    if form == "month":
        start, end = _month_bounds(arg)
        if source == "pubmed":
            return lambda: pubmed_works.fetch_month(start.year, start.month)
        return _range_fetch(source, start, end)
    if form == "quarter" and source == "odp":
        year, quarter = arg.split("-Q")
        return lambda: odp_patents.fetch_quarter(int(year), int(quarter))
    if form == "fy" and source == "reporter":
        fy = int(arg.split("@", 1)[0])
        return lambda: reporter.fetch_fiscal_year(fy)
    if form == "daily":
        day = date.fromisoformat(arg)
        if source == "pubmed":
            return lambda: pubmed_works.fetch_edat(day - timedelta(days=1), day)
        if source == "openalex":
            since = day - timedelta(days=DAILY_LOOKBACK_DAYS)
            return lambda: openalex_works.fetch_slice(since, day)
    if form == "sweep" and source == "openalex":
        start, end = _month_bounds(arg.split("/", 1)[1])
        return lambda: openalex_works.fetch_slice(start, end)
    if form == "week":
        monday = date.fromisoformat(arg)
        if source == "odp":
            year, quarter = _quarter_of(monday)
            return lambda: odp_patents.fetch_quarter(year, quarter)
        return _range_fetch(
            source, monday - timedelta(days=WEEKLY_LOOKBACK_DAYS), monday + timedelta(days=6)
        )
    raise ValueError(f"slice {slice_key!r} is not one source {source!r} takes")


# --- scheduling --------------------------------------------------------------------------------


def _window_months(today: date, window_days: int) -> list[tuple[int, int]]:
    """Every calendar month the window ending `today` touches, oldest first."""
    start = today - timedelta(days=window_days)
    months = []
    year, month = start.year, start.month
    while (year, month) <= (today.year, today.month):
        months.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def _fiscal_year(year: int, month: int) -> int:
    return year + 1 if month >= 10 else year


def _month_slices(year: int, month: int) -> list[tuple[str, str]]:
    """The slices that cover one calendar month in every source but JHTV."""
    _, quarter = _quarter_of(date(year, month, 1))
    slices = [(s, f"month:{year:04d}-{month:02d}") for s in _MONTH_SOURCES]
    slices.append(("odp", f"quarter:{year:04d}-Q{quarter}"))
    slices.append(("reporter", f"fy:{_fiscal_year(year, month)}"))
    return slices


def pilot_slices(*, today: date, seed: int) -> list[tuple[str, str]]:
    """The pilot intake (D68): one month from each calendar year the window touches, picked
    with ``random.Random(seed)`` among that year's in-window months, as each source's slice
    covering it (the ODP quarter and RePORTER fiscal year holding the month), plus the JHTV
    feed once. Every source is listed; `schedule_due` drops disabled ones."""
    months = _window_months(today, get_settings().landscape_window_days)
    rng = random.Random(seed)
    slices: list[tuple[str, str]] = []
    for year in sorted({y for y, _ in months}):
        year, month = rng.choice([(y, m) for y, m in months if y == year])
        slices += _month_slices(year, month)
    slices.append(("jhtv", "feed:pilot"))
    return list(dict.fromkeys(slices))


def backfill_slices(today: date, window_days: int) -> list[tuple[str, str]]:
    """Every slice of the window, oldest first (§15 Phase 1). The worker's D69 drain check
    reads the same list: the backfill has drained once each enabled source's slices ended."""
    slices: list[tuple[str, str]] = [("jhtv", "feed:backfill")]
    for year, month in _window_months(today, window_days):
        slices += _month_slices(year, month)
    return list(dict.fromkeys(slices))


def _continuous_slices(today: date, window_days: int) -> list[tuple[str, str]]:
    """The D17 cadence: daily PubMed, OpenAlex and JHTV; a weekly OpenAlex full-window sweep
    and weekly NSF, ODP, DataCite and GEO; a monthly RePORTER refresh of the current and
    previous fiscal years."""
    day = today.isoformat()
    monday = (today - timedelta(days=today.weekday())).isoformat()
    slices = [(s, f"daily:{day}") for s in ("pubmed", "openalex", "jhtv")]
    slices += [
        ("openalex", f"sweep:{monday}/{y:04d}-{m:02d}")
        for y, m in _window_months(today, window_days)
    ]
    slices += [(s, f"week:{monday}") for s in _WEEKLY_SOURCES]
    fy = _fiscal_year(today.year, today.month)
    slices += [("reporter", f"fy:{year}@{today:%Y-%m}") for year in (fy, fy - 1)]
    return slices


def _ncbi_off_peak(now: datetime) -> bool:
    """NCBI asks for large jobs on weekends or 9 pm-5 am US Eastern (K8)."""
    eastern = now.astimezone(_EASTERN)
    return eastern.weekday() >= 5 or eastern.hour >= 21 or eastern.hour < 5


async def _setting(db: AsyncSession, key: str) -> str | None:
    value = (await db.execute(
        select(AppSetting.value).where(AppSetting.key == key)
    )).scalar_one_or_none()
    return value.strip().strip('"') if value is not None else None


async def _pilot_anchor(db: AsyncSession, now: datetime) -> tuple[date, int]:
    """The pilot's fixed `today` and seed: the creation time of the first source row, so the
    random months stay the same across restarts and days. Before any row exists, `now`."""
    first = (await db.execute(select(func.min(LandscapeSource.created_at)))).scalar_one_or_none()
    anchor = first or now
    return anchor.date(), int(anchor.timestamp() * 1_000_000)


async def _settled(
    db: AsyncSession, wanted: list[tuple[str, str]], now: datetime,
) -> set[tuple[str, str]]:
    """The wanted slices not due: ended (``complete``/``incomplete``), running, or failed
    within `FAILED_RETRY_AFTER`."""
    if not wanted:
        return set()
    F = LandscapeFetch
    rows = (await db.execute(
        select(F.source, F.slice_key, F.status, F.started_at).where(
            F.source.in_({s for s, _ in wanted}), F.slice_key.in_({k for _, k in wanted}),
        )
    )).all()
    settled = set()
    for source, key, status, started in rows:
        if (
            status in ("complete", "incomplete")
            or (status == "running" and now - started < RUNNING_STALE_AFTER)
            or (status == "failed" and now - started < FAILED_RETRY_AFTER)
        ):
            settled.add((source, key))
    return settled


async def schedule_due(db: AsyncSession, *, now: datetime) -> list[tuple[str, str]]:
    """The ``(source, slice_key)`` pairs due at `now`, by ``app_settings.landscape_phase``:

    - ``pilot`` (also when unset, the state before an admin starts the backfill):
      `pilot_slices`, anchored by `_pilot_anchor` (D68);
    - ``backfill``: every monthly slice of the window, oldest first; PubMed and GEO only in
      NCBI's off-peak hours (K8);
    - ``continuous``: the D17 cadence.

    A disabled source is never due, nor is a slice already ended, running, or failed within
    the hour. The worker passes each pair to `slice_fetcher` and `run_slice`."""
    phase = await _setting(db, "landscape_phase") or "pilot"
    window_days = get_settings().landscape_window_days
    if phase == "pilot":
        anchor_day, seed = await _pilot_anchor(db, now)
        wanted = pilot_slices(today=anchor_day, seed=seed)
    elif phase == "backfill":
        wanted = backfill_slices(now.date(), window_days)
        if not _ncbi_off_peak(now):
            wanted = [(s, k) for s, k in wanted if s not in NCBI_SOURCES]
    elif phase == "continuous":
        wanted = _continuous_slices(now.date(), window_days)
    else:
        logger.warning("landscaping: unknown landscape_phase %r; nothing scheduled", phase)
        return []
    enabled = set((await db.execute(
        select(LandscapeSource.source).where(LandscapeSource.enabled.is_(True))
    )).scalars())
    wanted = [(s, k) for s, k in wanted if s in enabled]
    settled = await _settled(db, wanted, now)
    return [pair for pair in wanted if pair not in settled]


async def ensure_sources(db: AsyncSession) -> None:
    """Insert the ``landscape_sources`` rows that are missing, ``jhtv`` disabled until a
    person has read its terms of use (spec §15). An existing row is never changed. Commits."""
    for source in SOURCES:
        await db.execute(
            pg_insert(LandscapeSource)
            .values(id=uuid.uuid4(), source=source, enabled=source not in _DISABLED_AT_START)
            .on_conflict_do_nothing(index_elements=["source"])
        )
    await db.commit()


# --- links and positive controls ----------------------------------------------------------------


def _senior_name(doc: LandscapeDocument) -> str:
    names = (doc.details or {}).get("senior_names") or []
    return str(names[0].get("name") or "") if names and isinstance(names[0], dict) else ""


async def _refresh_grant_pmids(grants: list[LandscapeDocument], now: datetime) -> None:
    """Ask RePORTER for the PMIDs of every grant not asked within `GRANT_LINKS_REFRESH`, and
    keep them on the grant (``details["cited_pmids"]``)."""
    stale = []
    for grant in grants:
        checked = (grant.details or {}).get("cited_pmids_checked_at")
        core = (grant.external_ids or {}).get("core_project")
        if core and (not checked or now - datetime.fromisoformat(checked) >= GRANT_LINKS_REFRESH):
            stale.append(grant)
    if not stale:
        return
    links = await nih_reporter.publications_for_cores(
        sorted({g.external_ids["core_project"] for g in stale})
    )
    for grant in stale:
        details = dict(grant.details or {})
        details["cited_pmids"] = sorted(links.get(grant.external_ids["core_project"], set()))
        details["cited_pmids_checked_at"] = now.isoformat()
        grant.details = details


async def link_grants(db: AsyncSession) -> int:
    """Link qualifying grants to the stored papers RePORTER says they produced
    (``publications_for_cores``, spec §6.1), as ``grant_cites`` links (``doc_a`` the grant),
    and call `unify_grant_pi` for each new link between two qualifying records (§5.2).
    RePORTER is asked again per grant after `GRANT_LINKS_REFRESH`; links to papers stored
    since are made from the kept PMIDs. Returns the new links. Commits."""
    D = LandscapeDocument
    grants = list((await db.execute(
        select(D).where(D.kind == "grant", D.merged_into.is_(None), D.role_basis.is_not(None))
    )).scalars())
    await _refresh_grant_pmids(grants, datetime.now(UTC))
    pmids = {p for g in grants for p in (g.details or {}).get("cited_pmids") or []}
    papers = {}
    if pmids:
        papers = {d.pmid: d for d in (await db.execute(
            select(D).where(D.pmid.in_(pmids), D.merged_into.is_(None), D.kind.in_(_WORK_KINDS))
        )).scalars()}
    created = 0
    for grant in grants:
        for pmid in (grant.details or {}).get("cited_pmids") or []:
            paper = papers.get(pmid)
            if paper is None or not await _link(db, grant.id, paper.id, "grant_cites"):
                continue
            created += 1
            if grant.lab_key and paper.lab_key and paper.role_basis:
                await unify_grant_pi(
                    db, grant_key=grant.lab_key, paper_key=paper.lab_key,
                    grant_name=_senior_name(grant), paper_name=_senior_name(paper),
                )
    await db.commit()
    return created


async def link_datasets(db: AsyncSession) -> int:
    """Attach each stored DataCite or GEO dataset to the stored documents its related DOIs
    and PMIDs name, as ``dataset_of`` links (``doc_a`` the dataset; D3). A dataset never
    qualifies on its own; the link makes it evidence of the work. Returns the new links.
    Commits."""
    D = LandscapeDocument
    datasets = (await db.execute(
        select(D.id, D.details).where(D.kind == "dataset", D.merged_into.is_(None))
    )).all()
    related = [(ds_id, (details or {}).get("related_ids") or {}) for ds_id, details in datasets]
    dois = {str(d).lower() for _, rel in related for d in rel.get("doi") or []}
    pmids = {str(p) for _, rel in related for p in rel.get("pmid") or []}
    live = (D.merged_into.is_(None), D.kind != "dataset")
    by_doi: dict[str, uuid.UUID] = {}
    by_pmid: dict[str, uuid.UUID] = {}
    if dois:
        by_doi = dict((await db.execute(select(D.doi, D.id).where(D.doi.in_(dois), *live))).all())
    if pmids:
        by_pmid = dict(
            (await db.execute(select(D.pmid, D.id).where(D.pmid.in_(pmids), *live))).all()
        )
    created = 0
    for ds_id, rel in related:
        targets = {by_doi[d.lower()] for d in rel.get("doi") or [] if d.lower() in by_doi}
        targets |= {by_pmid[str(p)] for p in rel.get("pmid") or [] if str(p) in by_pmid}
        for target in targets:
            created += await _link(db, ds_id, target, "dataset_of")
    await db.commit()
    return created


def _parse_controls(raw: str | None) -> list[tuple[str, str]]:
    """``("doi" | "pmid", value)`` for each identifier in the setting: a JSON list of
    strings, or text separated by whitespace, commas or semicolons. Anything else is logged
    and skipped."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    tokens = [str(t) for t in parsed] if isinstance(parsed, list) else re.split(r"[\s,;]+", raw)
    out = []
    for token in (t.strip() for t in tokens):
        if not token:
            continue
        if doi := normalize_doi(token):
            out.append(("doi", doi.lower()))
        elif pmid := normalize_pmid(token):
            out.append(("pmid", pmid))
        else:
            logger.warning("landscaping: positive control %r is neither a DOI nor a PMID", token)
    return list(dict.fromkeys(out))


async def _openalex_control(scheme: str, value: str) -> DocumentRecord | None:
    """The OpenAlex work with this DOI or PMID, fetched again singly for its full author
    list (D39), or None."""
    async with contextlib.aclosing(
        openalex.iter_works(f"{scheme}:{value}", openalex_works.SELECT, per_page=1)
    ) as pages:
        async for rows, _count in pages:
            work = rows[0] if rows else None
            break
        else:
            work = None
    if work is None:
        return None
    full = await openalex.fetch_work(work.get("id") or "", openalex_works.SELECT)
    return openalex_works.to_document(full or work)


async def _pubmed_control(scheme: str, value: str) -> DocumentRecord | None:
    """The PubMed record with this PMID (a DOI converted first), or None."""
    pmid = value if scheme == "pmid" else (await pubmed.convert_dois_to_pmids([value])).get(value)
    if not pmid:
        return None
    xml_text = await pubmed.fetch_pubmed_xml([pmid])
    extras = pubmed.parse_pubmed_extras(xml_text)
    for record in pubmed._parse_pubmed_xml(xml_text):
        if str(record.get("pmid") or "") == pmid and pmid in extras:
            return pubmed_works.to_document(record, extras[pmid])
    return None


async def ingest_positive_controls(db: AsyncSession) -> int:
    """Store every ``app_settings.landscape_positive_controls`` DOI or PMID through S1, so
    the pilot can trace it (D68, spec §10.2). A control already stored is not fetched
    again; a missing one is fetched singly from OpenAlex, else PubMed, and upserted whether
    or not it qualifies, since S1 dropping it is part of the trace. A control neither source
    has is logged. Returns how many controls are now stored. Commits after each."""
    stored = 0
    for scheme, value in _parse_controls(await _setting(db, "landscape_positive_controls")):
        if await find_document(db, **{scheme: value}) is not None:
            stored += 1
            continue
        rec = await _openalex_control(scheme, value) or await _pubmed_control(scheme, value)
        if rec is None:
            logger.warning("landscaping: positive control %s:%s not found", scheme, value)
            continue
        await upsert_document(db, rec)
        await db.commit()
        stored += 1
    return stored
