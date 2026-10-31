"""S3 extract from summaries: one batched call per screened-in document and epoch (spec §7.4).

A document S2 answered ``yes`` or ``unsure``, or whose S2 ``no`` staff rescued (§10.5, L44), gets
a ``summary``-basis extraction row. Its result
is quote-checked against the text S2 read (§7.4 step 3), its targets, disease and agent are
normalized (step 4), and each idea candidate is merged into the epoch's ideas (step 7). For a
patent or a technology-transfer listing, each ``linked_publications`` entry found verbatim in
the text and resolved to a stored document becomes an extraction link in the epoch (D67).

Every derived row is written in the transaction of the result's compare-and-set, and only when
it changed the row (N12), so a replayed chunk writes nothing twice. Normalization calls outside
services, so on the batch path it runs before that transaction opens; a fallback re-run
normalizes inside ``run_fallbacks``' transaction, which owns the row lock.

Requests, and the criteria normalization reads, come from the row's epoch's own inputs
(``epochs.epoch_inputs``), never the files on disk or the current settings (spec §7.11).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.landscaping import batches, epochs, normalize
from src.landscaping.batches import BatchOutcome, FallbackAdapter
from src.landscaping.criteria import Criteria
from src.landscaping.evidence import pubtator
from src.landscaping.ideas import merge_candidate
from src.landscaping.intake import find_document
from src.landscaping.llm_client import CallResult, build_request
from src.landscaping.quotes import apply_quote_check, verify_quote
from src.landscaping.schemas import SCHEMAS
from src.landscaping.stages.screen import (
    document_text,
    fallback_build,
    message_text,
    parse_output,
    served_by,
    submit_items,
    user_message,
)
from src.models.landscape import (
    LandscapeDocument,
    LandscapeExtraction,
    LandscapeExtractionLink,
    LandscapeScreen,
)
from src.services.job_queue import job_error_text

logger = logging.getLogger(__name__)

#: Document kinds whose extraction names supporting publications (D67).
LINKING_KINDS = ("patent", "jhtv")
_PUBLICATION_FIELDS = ("doi", "pmid", "title")
_SUBMIT_READ_LIMIT = 20_000
_INSERT_CHUNK = 1000


@dataclass
class _Prepared:
    """A checked and normalized extraction, ready to store."""

    cleaned: dict
    verified: int
    dropped: int
    candidates: list[dict]
    publications: list[dict]


def _request(doc: LandscapeDocument, *, model: str, system: str, retry: bool) -> dict:
    return build_request(
        "extract", model=model, system=system, user=user_message(doc),
        schema=SCHEMAS["extract"], retry=retry,
    )


# --- submission ------------------------------------------------------------------------------


def _screened_in(epoch_id: uuid.UUID):
    """The join condition: the document's S2 row in the epoch is done with yes or unsure, or
    rescued (a rescued ``no`` re-enters at the next stage, L44)."""
    S = LandscapeScreen
    return and_(
        S.document_id == LandscapeDocument.id,
        S.epoch_id == epoch_id,
        or_(and_(S.status == "done", S.decision.in_(("yes", "unsure"))), S.status == "rescued"),
    )


async def _ensure_rows(db: AsyncSession, epoch_id: uuid.UUID) -> None:
    """A ``pending`` summary-basis row for every screened-in, non-tombstone document without
    one in the epoch. An existing row, ``pending`` after an intake reset, is re-run in place."""
    D, E = LandscapeDocument, LandscapeExtraction
    has_row = (
        select(E.id)
        .where(E.document_id == D.id, E.epoch_id == epoch_id, E.basis == "summary")
        .exists()
    )
    doc_ids = (
        await db.scalars(
            select(D.id)
            .join(LandscapeScreen, _screened_in(epoch_id))
            .where(D.merged_into.is_(None), ~has_row)
        )
    ).all()
    for start in range(0, len(doc_ids), _INSERT_CHUNK):
        chunk = doc_ids[start:start + _INSERT_CHUNK]
        await db.execute(
            pg_insert(E)
            .values([
                {"id": uuid.uuid4(), "document_id": doc_id, "epoch_id": epoch_id,
                 "basis": "summary"}
                for doc_id in chunk
            ])
            .on_conflict_do_nothing(index_elements=["document_id", "epoch_id", "basis"])
        )


async def submit_extract_batch(db_factory, epoch_id: uuid.UUID) -> int:
    """Batch S3 for the epoch's documents whose S2 answer is ``yes`` or ``unsure``, or rescued.

    First creates the missing ``pending`` rows. A pending row is sent only while its S2 row is
    still done with yes or unsure, or rescued, so a document whose summary changed waits for
    its new screen. Rows waiting for the fallback chain are never batched (D42). Requests use the
    epoch's own prompt and model (§7.11). Returns the number of requests submitted.
    """
    D, E = LandscapeDocument, LandscapeExtraction
    async with db_factory() as db:
        inputs = await epochs.epoch_inputs(db, epoch_id)
        model, system = inputs.model("extract"), inputs.system("extract")
        await _ensure_rows(db, epoch_id)
        await db.commit()
        rows = (
            await db.execute(
                select(E.id, E.truncated_once, D)
                .join(D, D.id == E.document_id)
                .join(LandscapeScreen, _screened_in(epoch_id))
                .where(
                    E.epoch_id == epoch_id,
                    E.basis == "summary",
                    E.status == "pending",
                    E.fallback_from.is_(None),
                    D.merged_into.is_(None),
                )
                .order_by(D.created_at, D.id)
                .limit(_SUBMIT_READ_LIMIT)
            )
        ).all()
    items = [
        (row_id, _request(doc, model=model, system=system, retry=truncated_once))
        for row_id, truncated_once, doc in rows
    ]
    return await submit_items(db_factory, stage="extract", row_model=E, items=items)


# --- checking, normalizing, storing ----------------------------------------------------------


async def _pubtator_for(doc: LandscapeDocument) -> list[dict]:
    """The document's PubTator3 annotations, normalization's first choice (§7.4 step 4). A
    failed lookup gives none, so names fall back to the HGNC rule or stay unmatched."""
    if not doc.pmid:
        return []
    try:
        found = await pubtator.pubtator_annotations([doc.pmid])
    except Exception as exc:  # noqa: BLE001 - normalization proceeds without annotations
        logger.warning("PubTator lookup failed for document %s: %s", doc.id, exc)
        return []
    return list(found.get(doc.pmid) or [])


async def _normalized(idea: dict, *, annotations: list[dict], criteria) -> dict:
    """``idea`` with its targets, disease and agent normalized; an empty disease or agent
    is left as written."""
    candidate = dict(idea)
    candidate["targets"] = await normalize.normalize_targets(
        list(idea.get("targets") or []), pubtator=annotations, criteria=criteria
    )
    disease = idea.get("disease") or {}
    if (disease.get("as_written") or "").strip():
        candidate["disease"] = await normalize.normalize_disease(disease, pubtator=annotations)
    agent = idea.get("agent") or {}
    if (agent.get("as_written") or "").strip():
        candidate["agent"] = await normalize.normalize_agent(agent)
    return candidate


def _verbatim_publications(entries: Any, text: str) -> list[dict]:
    """Each entry's DOI, PMID and title that appear verbatim in ``text``; entries with none
    left are dropped (D67)."""
    kept = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        found = {
            field: value.strip()
            for field in _PUBLICATION_FIELDS
            if isinstance(value := entry.get(field), str) and value.strip()
            and verify_quote(value.strip(), text)
        }
        if found:
            kept.append(found)
    return kept


async def _prepare(parsed: dict, doc: LandscapeDocument, criteria: Criteria) -> _Prepared:
    """The quote check, normalization (with the epoch's ``criteria``) and the verbatim
    publications of one parsed extraction."""
    text = document_text(doc)
    cleaned, verified, dropped = apply_quote_check(parsed, text)
    annotations = await _pubtator_for(doc)
    candidates = [
        await _normalized(idea, annotations=annotations, criteria=criteria)
        for idea in cleaned.get("ideas") or []
        if isinstance(idea, dict)
    ]
    publications = (
        _verbatim_publications(cleaned.get("linked_publications"), text)
        if doc.kind in LINKING_KINDS else []
    )
    return _Prepared(cleaned, verified, dropped, candidates, publications)


def _success_values(prepared: _Prepared, doc: LandscapeDocument, model: str) -> dict:
    return {
        "status": "done",
        "raw": prepared.cleaned,
        "quotes_total": prepared.verified + prepared.dropped,
        "quotes_verified": prepared.verified,
        "summary_hash": doc.summary_hash,
        "served_by_model": model,
        "error": None,
    }


def _invalid_values() -> dict:
    return {"status": "failed:invalid", "error": job_error_text("extract output is not valid")}


def _is_extraction(parsed: dict | None) -> bool:
    return parsed is not None and isinstance(parsed.get("ideas"), list)


async def _write_derived(
    db: AsyncSession, *, extraction_id: uuid.UUID, epoch_id: uuid.UUID,
    doc: LandscapeDocument, prepared: _Prepared,
) -> None:
    """The extraction's ideas and links, in the caller's transaction (N12)."""
    for index, candidate in enumerate(prepared.candidates):
        await merge_candidate(
            db, epoch_id=epoch_id, extraction_id=extraction_id, index=index,
            candidate=candidate, document=doc,
        )
    for entry in prepared.publications:
        linked = await find_document(
            db, doi=entry.get("doi"), pmid=entry.get("pmid"), title=entry.get("title")
        )
        if linked is None or linked == doc.id:
            continue
        await db.execute(
            pg_insert(LandscapeExtractionLink)
            .values(
                id=uuid.uuid4(), epoch_id=epoch_id, doc_a=doc.id, doc_b=linked,
                extraction_id=extraction_id,
            )
            .on_conflict_do_nothing(index_elements=["epoch_id", "doc_a", "doc_b"])
        )


# --- ingestion -------------------------------------------------------------------------------


async def _ingest_one(db_factory, outcome: BatchOutcome) -> None:
    async with db_factory() as db:
        row = await db.get(LandscapeExtraction, outcome.row_id)
        # A replayed or superseded result: skip the normalization calls; the compare-and-set
        # below would change nothing anyway.
        if row is None or row.status != "submitted" or row.batch_item_id != outcome.batch_item_id:
            return
        doc = await db.get(LandscapeDocument, row.document_id)
        attempts, truncated_once, epoch_id = row.attempts, row.truncated_once, row.epoch_id
        criteria = (
            (await epochs.epoch_inputs(db, epoch_id)).criteria
            if outcome.kind == "succeeded" else None
        )
    prepared = None
    if outcome.kind == "succeeded":
        parsed = parse_output(message_text(outcome.message))
        if _is_extraction(parsed) and doc is not None:
            prepared = await _prepare(parsed, doc, criteria)
            values = _success_values(prepared, doc, served_by(outcome))
        else:
            values = _invalid_values()
    else:
        values = batches.transition(
            outcome.kind, attempts=attempts, truncated_once=truncated_once, model=outcome.model
        )
    async with db_factory() as db:
        changed = await batches.apply_result(
            db, LandscapeExtraction, row_id=outcome.row_id, batch_item_id=outcome.batch_item_id,
            values=values,
        )
        if changed and prepared is not None:
            await _write_derived(
                db, extraction_id=outcome.row_id, epoch_id=epoch_id,
                doc=await db.get(LandscapeDocument, doc.id), prepared=prepared,
            )
        await db.commit()


async def ingest_extract_outcomes(db_factory, outcomes: list[BatchOutcome]) -> None:
    """Apply S3 batch results, each a compare-and-set in its own transaction (§7.1 step 5).

    ``succeeded``: the quote check, normalization, then the row's values with the merged ideas
    and the extraction links (D67), all in one transaction and only when the compare-and-set
    changed the row. A reply that is not an extraction is ``failed:invalid``. Every other kind
    takes ``batches.transition``; a refusal leaves the row for ``FALLBACKS``.
    """
    for outcome in outcomes:
        await _ingest_one(db_factory, outcome)


async def _on_fallback_success(
    db: AsyncSession, row: LandscapeExtraction, result: CallResult
) -> None:
    """Store a fallback extraction and its derived rows in ``run_fallbacks``' transaction."""
    doc = await db.get(LandscapeDocument, row.document_id)
    parsed = parse_output(result.text)
    if not _is_extraction(parsed) or doc is None:
        for key, value in _invalid_values().items():
            setattr(row, key, value)
        return
    criteria = (await epochs.epoch_inputs(db, row.epoch_id)).criteria
    prepared = await _prepare(parsed, doc, criteria)
    for key, value in _success_values(prepared, doc, result.served_by).items():
        setattr(row, key, value)
    await _write_derived(
        db, extraction_id=row.id, epoch_id=row.epoch_id, doc=doc, prepared=prepared
    )


FALLBACKS: tuple[FallbackAdapter, ...] = (
    FallbackAdapter(
        stage="extract", row_model=LandscapeExtraction,
        build=fallback_build("extract", _request), on_success=_on_fallback_success,
    ),
)
