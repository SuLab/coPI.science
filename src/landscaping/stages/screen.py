"""S2 screen: one batched call per eligible document and epoch (spec §7.3).

The screen reads what a document says about itself (title, summary, a grant's public health
relevance, a patent's first claim) and never a name, institution or journal. ``yes`` and
``unsure`` go on to S3; ``no`` is ``excluded:screen`` (recall first, analysis F3).

This module also holds what S2 and S3 share: the document text both read (S3's quote check runs
against the same text), its fencing (D44), the batch sizing and the fallback request builder.

Every request takes its system prompt and model from the row's epoch's own inputs
(``epochs.epoch_inputs``), never the files on disk or the current settings, so an epoch's
judgments are never mixed (spec §7.11). A refused row's fallback request is rebuilt the same
way, from its document, in ``FallbackAdapter.build`` (D42).
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.prompt_safety import delimit
from src.landscaping import batches, epochs
from src.landscaping.batches import BatchOutcome, FallbackAdapter
from src.landscaping.llm_client import CallResult, build_request
from src.landscaping.schemas import SCHEMAS
from src.models.landscape import LandscapeDocument, LandscapeScreen
from src.services.job_queue import job_error_text
from src.services.json_extract import extract_json

logger = logging.getLogger(__name__)

#: The Message Batches limits are 100,000 requests and 256 MB per batch (spec §7.1); the byte
#: budget leaves headroom for the JSONL framing around each request.
BATCH_MAX_REQUESTS = 100_000
BATCH_MAX_BYTES = 200 * 1024 * 1024
#: Rows read per submission call; what does not fit waits for the next call.
_SUBMIT_READ_LIMIT = 20_000
_INSERT_CHUNK = 1000

#: How the user message names each document kind; the extraction prompt fills
#: ``linked_publications`` only for a patent or a technology-transfer listing.
KIND_LABELS = {
    "work": "research article",
    "preprint": "preprint",
    "grant": "grant",
    "patent": "patent",
    "jhtv": "technology-transfer listing",
    "dataset": "dataset",
}


# --- the document text S2 and S3 read --------------------------------------------------------


def _first_claim(claims: Any) -> str | None:
    """The first claim of a patent's stored ``claims``, a string or a list of strings or
    ``{"text": …}`` objects, depending on the record."""
    first = claims[0] if isinstance(claims, list) and claims else claims
    if isinstance(first, dict):
        first = first.get("text") or first.get("claimText")
    return first.strip() if isinstance(first, str) and first.strip() else None


def document_text(doc: LandscapeDocument) -> str:
    """The text S2 judges and S3 extracts from and quote-checks (spec §7.3, §7.4 step 1).

    Title, then the stored summary under the section name the extraction prompt uses
    (``description`` for a technology-transfer listing, else ``abstract``), then a grant's
    public health relevance or a patent's first claim when stored. No author, institution,
    journal or grant-holder name: ``details`` contributes only those two fields.
    """
    extra = (doc.details or {}).get("extra") or {}
    parts = []
    if doc.title and doc.title.strip():
        parts.append(f"Title: {doc.title.strip()}")
    summary = (doc.summary_text or "").strip()
    if summary:
        parts.append(f"{'Description' if doc.kind == 'jhtv' else 'Abstract'}: {summary}")
    if doc.kind == "grant":
        relevance = extra.get("phr_text")
        if isinstance(relevance, str) and relevance.strip() and relevance.strip() != summary:
            parts.append(f"Public health relevance: {relevance.strip()}")
    if doc.kind == "patent" and (claim := _first_claim(extra.get("claims"))):
        parts.append(f"First claim: {claim}")
    return "\n\n".join(parts)


def fenced_text(doc: LandscapeDocument) -> str:
    """The document's text fenced with its registered tag (D44): ``patent`` for a patent,
    else ``publication_abstract``. Each tag is a literal at its call, which
    tests/unit/test_prompt_safety.py checks against FENCE_TAGS."""
    if doc.kind == "patent":
        return delimit(document_text(doc), "patent")
    return delimit(document_text(doc), "publication_abstract")


def user_message(doc: LandscapeDocument) -> str:
    """The S2/S3 user message: the document's kind, then its fenced text."""
    label = KIND_LABELS.get(doc.kind, doc.kind)
    return f"Document kind: {label}\n\n{fenced_text(doc)}"


def message_text(message: Any) -> str:
    return "".join(
        block.text for block in (getattr(message, "content", None) or [])
        if getattr(block, "type", None) == "text"
    )


def parse_output(text: str) -> dict | None:
    """The model's JSON object (``extract_json``), or None when there is none."""
    try:
        parsed = extract_json(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def served_by(outcome: BatchOutcome) -> str:
    return getattr(outcome.message, "model", None) or outcome.model


# --- batching --------------------------------------------------------------------------------


def fit_batch(items: list[tuple[uuid.UUID, dict]]) -> list[tuple[uuid.UUID, dict]]:
    """The leading ``items`` that fit one Message Batch (``BATCH_MAX_REQUESTS``,
    ``BATCH_MAX_BYTES``); the rest wait for the next submission."""
    kept: list[tuple[uuid.UUID, dict]] = []
    size = 0
    for row_id, request in items[:BATCH_MAX_REQUESTS]:
        size += len(json.dumps(request).encode("utf-8"))
        if size > BATCH_MAX_BYTES:
            break
        kept.append((row_id, request))
    return kept


async def submit_items(
    db_factory, *, stage: str, row_model: type, items: list[tuple[uuid.UUID, dict]]
) -> int:
    """Submit what fits of ``items`` as one batch; the number submitted (0 when none)."""
    kept = fit_batch(items)
    if not kept:
        return 0
    try:
        await batches.submit(db_factory, stage=stage, row_model=row_model, items=kept)
    except ValueError:
        # Every row changed since it was read (an intake reset, say): nothing to send.
        return 0
    return len(kept)


def fallback_build(stage: str, request: Callable[..., dict]):
    """A ``FallbackAdapter.build`` for a document-keyed stage row (S2, S3): ``request(doc,
    model=, system=, retry=)`` over the row's document, in the row's epoch's inputs. Raises
    ``LookupError`` when the document is gone or the epoch has no snapshot."""

    async def build(db: AsyncSession, row: Any) -> dict:
        doc = await db.get(LandscapeDocument, row.document_id)
        if doc is None:
            raise LookupError(f"{stage} row {row.id}: its document is gone")
        inputs = await epochs.epoch_inputs(db, row.epoch_id)
        return request(
            doc, model=inputs.model(stage), system=inputs.system(stage),
            retry=row.truncated_once,
        )

    return build


# --- S2 --------------------------------------------------------------------------------------


def _request(doc: LandscapeDocument, *, model: str, system: str, retry: bool) -> dict:
    return build_request(
        "screen", model=model, system=system, user=user_message(doc), schema=SCHEMAS["screen"],
        retry=retry,
    )


async def _ensure_rows(db: AsyncSession, epoch_id: uuid.UUID) -> None:
    """A ``pending`` screen row for every eligible, non-tombstone document without one in the
    epoch. An existing row is never duplicated (intake resets it in place)."""
    D, S = LandscapeDocument, LandscapeScreen
    has_row = select(S.id).where(S.document_id == D.id, S.epoch_id == epoch_id).exists()
    doc_ids = (
        await db.scalars(
            select(D.id).where(D.s1_status == "eligible", D.merged_into.is_(None), ~has_row)
        )
    ).all()
    for start in range(0, len(doc_ids), _INSERT_CHUNK):
        chunk = doc_ids[start:start + _INSERT_CHUNK]
        await db.execute(
            pg_insert(S)
            .values([
                {"id": uuid.uuid4(), "document_id": doc_id, "epoch_id": epoch_id}
                for doc_id in chunk
            ])
            .on_conflict_do_nothing(index_elements=["document_id", "epoch_id"])
        )


async def submit_screen_batch(db_factory, epoch_id: uuid.UUID) -> int:
    """Batch S2 for the epoch's documents with S1 ``eligible`` and S2 ``pending``.

    First creates the missing ``pending`` rows. Rows waiting for the fallback chain are never
    batched (D42). Requests use the epoch's own prompt and model (§7.11). A row truncated once
    is sent with the stage's higher limit (D32). Returns the number of requests submitted.
    """
    D, S = LandscapeDocument, LandscapeScreen
    async with db_factory() as db:
        inputs = await epochs.epoch_inputs(db, epoch_id)
        model, system = inputs.model("screen"), inputs.system("screen")
        await _ensure_rows(db, epoch_id)
        await db.commit()
        rows = (
            await db.execute(
                select(S.id, S.truncated_once, D)
                .join(D, D.id == S.document_id)
                .where(
                    S.epoch_id == epoch_id,
                    S.status == "pending",
                    S.fallback_from.is_(None),
                    D.s1_status == "eligible",
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
    return await submit_items(db_factory, stage="screen", row_model=S, items=items)


def _result_values(parsed: dict | None, doc: LandscapeDocument | None, model: str) -> dict:
    """Stage-row values for a screen answer; ``failed:invalid`` when it is not one."""
    decision = parsed.get("in_scope") if parsed is not None else None
    if decision not in ("yes", "no", "unsure"):
        return {"status": "failed:invalid", "error": job_error_text("screen output is not valid")}
    return {
        "status": "excluded:screen" if decision == "no" else "done",
        "decision": decision,
        "kinds": list(parsed.get("research_kind") or []),
        "mentions": {
            "targets": list(parsed.get("targets_mentioned") or []),
            "diseases": list(parsed.get("diseases_mentioned") or []),
            "modality": parsed.get("modality_mentioned"),
        },
        "reason": parsed.get("reason"),
        "summary_hash": doc.summary_hash if doc is not None else None,
        "served_by_model": model,
        "error": None,
    }


async def ingest_screen_outcomes(db_factory, outcomes: list[BatchOutcome]) -> None:
    """Apply S2 batch results, each a compare-and-set in its own transaction (spec §7.1 step 5).

    ``succeeded`` stores the answer; every other kind takes ``batches.transition`` (a refusal
    leaves the row for ``FALLBACKS``). A replayed chunk changes nothing.
    """
    for outcome in outcomes:
        async with db_factory() as db:
            row = await db.get(LandscapeScreen, outcome.row_id)
            if row is None:
                continue
            doc = await db.get(LandscapeDocument, row.document_id)
            if outcome.kind == "succeeded":
                parsed = parse_output(message_text(outcome.message))
                values = _result_values(parsed, doc, served_by(outcome))
            else:
                values = batches.transition(
                    outcome.kind, attempts=row.attempts, truncated_once=row.truncated_once,
                    model=outcome.model,
                )
            await batches.apply_result(
                db, LandscapeScreen, row_id=outcome.row_id, batch_item_id=outcome.batch_item_id,
                values=values,
            )
            await db.commit()


async def _on_fallback_success(db: AsyncSession, row: LandscapeScreen, result: CallResult) -> None:
    doc = await db.get(LandscapeDocument, row.document_id)
    for key, value in _result_values(parse_output(result.text), doc, result.served_by).items():
        setattr(row, key, value)


FALLBACKS: tuple[FallbackAdapter, ...] = (
    FallbackAdapter(
        stage="screen", row_model=LandscapeScreen,
        build=fallback_build("screen", _request), on_success=_on_fallback_success,
    ),
)
