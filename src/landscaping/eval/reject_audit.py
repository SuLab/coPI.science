"""The reject audit: a second opinion on a sample of each cycle's rejects (spec §10.5, D66).

Once per cycle, a random sample of ``landscape_reject_audit_size`` per stage of the epoch's S2
``no`` documents (``excluded:screen``) and S4 exclusions (``excluded:<reason>``) is re-judged in
one Message Batch. "This cycle's" rejects are those excluded since the stage's previous audit
in the epoch; an item is audited at most once per epoch (the table's unique key).

Each request is built by the audited stage's own builder (``build_request`` with that stage's
effort, limits and schema), with the system prompt from the epoch's inputs (§7.11), on
``landscape_audit_model``. The audit model is read from the settings, not the epoch: it is not
a judging input (D61). Batch rows and fallback re-runs are ledgered as ``audit`` (D66).

An audit row records the original and the audit decision and whether they agree, meaning the
audit rejects too (an S4 audit that excludes for another reason still agrees).
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.landscaping import batches, epochs
from src.landscaping.batches import BatchOutcome, FallbackAdapter
from src.landscaping.epochs import EpochInputs
from src.landscaping.llm_client import STAGE_SPECS, CallResult, build_request
from src.landscaping.schemas import SCHEMAS, stage_schema
from src.landscaping.scoring import excluded_reason
from src.landscaping.stages.screen import message_text, parse_output, submit_items, user_message
from src.models.landscape import (
    LandscapeDocument,
    LandscapeRejectAudit,
    LandscapeScreen,
    LandscapeTriage,
)
from src.services.job_queue import job_error_text

#: The ledger stage of every audit call, batched or synchronous (D66).
LEDGER_STAGE = "audit"
#: Audited stage-row kind -> its row model. Each kind is also the stage whose builder the
#: audit uses.
AUDITED = {"screen": LandscapeScreen, "triage": LandscapeTriage}
#: The stage ``run_fallbacks`` passes to ``call_sync`` for every audit row. One adapter serves
#: both kinds (they share one table), so it takes the kind with the higher retry limit: a
#: truncated re-run is then never cut shorter than its own stage's retry limit.
_FALLBACK_STAGE = max(AUDITED, key=lambda stage: STAGE_SPECS[stage].retry_max_tokens)


def _excluded(kind: str, status_column: Any) -> Any:
    """The SQL test for a reject at ``kind``'s stage: S2 ``no``, or any S4 exclusion."""
    if kind == "screen":
        return status_column == "excluded:screen"
    return status_column.startswith("excluded:")


# --- sampling -------------------------------------------------------------------------------


async def _sample(db: AsyncSession, kind: str, epoch_id: uuid.UUID, size: int) -> None:
    """Create ``pending`` audit rows for a random sample of up to ``size`` of the epoch's
    ``kind`` rejects excluded since that stage's previous audit and never audited."""
    model, audit = AUDITED[kind], LandscapeRejectAudit
    since = await db.scalar(
        select(func.max(audit.created_at))
        .where(audit.epoch_id == epoch_id, audit.item_kind == kind)
    )
    audited = (
        select(audit.id)
        .where(audit.item_kind == kind, audit.item_id == model.id, audit.epoch_id == epoch_id)
        .exists()
    )
    query = select(model.id, model.status).where(
        model.epoch_id == epoch_id, _excluded(kind, model.status), ~audited
    )
    if since is not None:
        query = query.where(model.updated_at > since)
    picked = (await db.execute(query.order_by(func.random()).limit(size))).all()
    if not picked:
        return
    await db.execute(
        pg_insert(audit)
        .values([
            {
                "id": uuid.uuid4(), "item_kind": kind, "item_id": row_id, "epoch_id": epoch_id,
                "original_decision": "no" if kind == "screen" else status,
            }
            for row_id, status in picked
        ])
        .on_conflict_do_nothing(constraint="uq_landscape_reject_audits_item_epoch")
    )


# --- requests -------------------------------------------------------------------------------


async def _request(
    db: AsyncSession, row: LandscapeRejectAudit, *, inputs: EpochInputs, model: str
) -> dict | None:
    """The audited stage's request for the audited row, on ``model``; None when its document
    or stored dossier is gone."""
    retry = row.truncated_once
    if row.item_kind == "screen":
        screened = await db.get(LandscapeScreen, row.item_id)
        doc = await db.get(LandscapeDocument, screened.document_id) if screened else None
        if doc is None:
            return None
        return build_request(
            "screen", model=model, system=inputs.system("screen"), user=user_message(doc),
            schema=SCHEMAS["screen"], retry=retry,
        )
    triaged = await db.get(LandscapeTriage, row.item_id)
    dossier = (triaged.raw or {}).get("dossier") if triaged is not None else None
    if not dossier:
        return None
    return build_request(
        "triage", model=model, system=inputs.system("triage"), user=dossier,
        schema=stage_schema("triage", inputs.rubric), retry=retry,
    )


async def submit_reject_audit(db_factory, *, epoch_id: uuid.UUID) -> int:
    """Sample this cycle's rejects of the epoch and submit every ``pending`` audit row of it
    (new samples, and rows sent back to ``pending`` by an errored, expired, canceled or
    truncated result) as one Message Batch at stage ``audit`` (D66).

    A row whose audited document or dossier is gone becomes ``failed:missing``. Rows waiting
    for the fallback chain are never batched (D42). Returns the number of requests submitted.
    """
    settings = get_settings()
    audit = LandscapeRejectAudit
    async with db_factory() as db:
        inputs = await epochs.epoch_inputs(db, epoch_id)
        for kind in AUDITED:
            await _sample(db, kind, epoch_id, settings.landscape_reject_audit_size)
        rows = (
            await db.scalars(
                select(audit)
                .where(
                    audit.epoch_id == epoch_id,
                    audit.status == "pending",
                    audit.fallback_from.is_(None),
                )
                .order_by(audit.created_at, audit.id)
            )
        ).all()
        items: list[tuple[uuid.UUID, dict]] = []
        for row in rows:
            request = await _request(db, row, inputs=inputs, model=settings.landscape_audit_model)
            if request is None:
                row.status = "failed:missing"
                row.updated_at = func.now()
                row.error = job_error_text(f"audited {row.item_kind} row {row.item_id} is gone")
            else:
                items.append((row.id, request))
        await db.commit()
    return await submit_items(db_factory, stage=LEDGER_STAGE, row_model=audit, items=items)


# --- results --------------------------------------------------------------------------------


def _triage_reason(parsed: dict, reason: str | None) -> str | None:
    if reason == "excluded:gate":
        failed = sorted(k for k, v in (parsed.get("gating") or {}).items() if v == "not_met")
        return "gate not met: " + ", ".join(failed)
    if reason == "excluded:scope":
        return "outside the therapeutic scope"
    return None


def _result_values(kind: str, text: str) -> dict:
    """Audit-row values for a reply: the audit decision and the agree flag, or
    ``failed:invalid`` when the reply is not an answer of the audited stage's shape."""
    parsed = parse_output(text)
    invalid = {"status": "failed:invalid", "error": job_error_text("audit output is not valid")}
    if kind == "screen":
        decision = parsed.get("in_scope") if parsed is not None else None
        if decision not in ("yes", "no", "unsure"):
            return invalid
        reason = parsed.get("reason")
        values = {
            "audit_decision": decision, "agree": decision == "no",
            "reason": reason if isinstance(reason, str) else None,
        }
    else:
        if parsed is None:
            return invalid
        excluded = excluded_reason(parsed)
        values = {
            "audit_decision": excluded or "pass", "agree": excluded is not None,
            "reason": _triage_reason(parsed, excluded),
        }
    return {**values, "status": "done", "raw": {"output": parsed}, "error": None}


async def ingest_audit_outcomes(db_factory, outcomes: list[BatchOutcome]) -> None:
    """Apply one chunk of audit outcomes through ``batches.apply_result`` (compare-and-set), so
    a replayed chunk changes nothing. ``succeeded`` stores the audit decision and the agree
    flag; other kinds take ``batches.transition`` (a refusal leaves the row for
    ``FALLBACKS``)."""
    async with db_factory() as db:
        for outcome in outcomes:
            row = await db.get(LandscapeRejectAudit, outcome.row_id)
            if row is None:
                continue
            if outcome.kind == "succeeded":
                values = _result_values(row.item_kind, message_text(outcome.message))
                served = getattr(outcome.message, "model", None) or outcome.model
                values["served_by_model"] = served
            else:
                values = batches.transition(
                    outcome.kind, attempts=row.attempts, truncated_once=row.truncated_once,
                    model=outcome.model,
                )
            await batches.apply_result(
                db, LandscapeRejectAudit, row_id=outcome.row_id,
                batch_item_id=outcome.batch_item_id, values=values,
            )
        await db.commit()


# --- the fallback chain (D42) ---------------------------------------------------------------


async def _fallback_request(db: AsyncSession, row: LandscapeRejectAudit) -> dict:
    """The row's audit request, rebuilt from the audited row in its epoch's inputs; raises
    ``LookupError`` when the audited document or dossier is gone."""
    inputs = await epochs.epoch_inputs(db, row.epoch_id)
    request = await _request(db, row, inputs=inputs, model=get_settings().landscape_audit_model)
    if request is None:
        raise LookupError(f"audit row {row.id}: its audited {row.item_kind} row is gone")
    return request


async def _fallback_success(
    db: AsyncSession, row: LandscapeRejectAudit, result: CallResult
) -> None:
    for column, value in _result_values(row.item_kind, result.text).items():
        setattr(row, column, value)


FALLBACKS: tuple[FallbackAdapter, ...] = (
    FallbackAdapter(
        stage=_FALLBACK_STAGE, row_model=LandscapeRejectAudit, build=_fallback_request,
        on_success=_fallback_success, ledger_stage=LEDGER_STAGE,
    ),
)
