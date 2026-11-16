"""S4 triage (batch; spec §7.5, L12, L23, L24, D10, D20).

An idea version is submitted once its evidence assembly has ended (every source in
``evidence_status`` ``done`` or ``failed``; a failed source is marked unavailable in the
dossier, so assembly never holds the funnel up, §7.4 step 5). The scores, the checklist
blend and the exclusion are computed in code (``src.landscaping.scoring``), never taken from
the model.

A triage row's ``raw`` holds ``{"dossier": <the user message sent>}`` from submission on, and
``"output"`` (the parsed model JSON) once a result is ingested; the fallback chain rebuilds its
request from the stored dossier (D42).

The system prompt, the model, and the rubric and criteria the score is computed with are the
row's epoch's own (``epochs.epoch_inputs``), never the files on disk or the current settings
(spec §7.11).
"""
from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.prompt_safety import delimit
from src.landscaping import batches, epochs
from src.landscaping.batches import BatchOutcome, FallbackAdapter
from src.landscaping.epochs import EpochInputs
from src.landscaping.ideas import dossier_documents
from src.landscaping.llm_client import CallResult, build_request
from src.landscaping.schemas import stage_schema
from src.landscaping.scoring import (
    TRIAGE_DIMENSIONS,
    excluded_reason,
    present_items,
    triage_score,
)
from src.models.landscape import (
    LandscapeDocument,
    LandscapeExtraction,
    LandscapeIdea,
    LandscapeIdeaEvidence,
    LandscapeTriage,
)
from src.services.job_queue import job_error_text
from src.services.json_extract import extract_json

logger = logging.getLogger(__name__)

STAGE = "triage"
_ASSEMBLY_ENDED = ("done", "failed")
_QUERY_CHUNK = 500

# Keys never rendered from an idea's structured facets: the dossier is blind (D20).
_IDENTITY_KEYS = frozenset({
    "lab", "labs", "lab_key", "senior_lab", "author", "authors", "affiliation", "affiliations",
    "institution", "institutions", "journal", "venue", "pi", "pi_name", "investigators",
})

# The model-output fields stored on the triage row as given.
_STORED_FIELDS = (
    "gating", "gating_rationales", "dimension_rationales", "target_rubric", "evidence_grade",
    "stage", "llm_flags", "red_flags", "confidence", "in_therapeutic_scope", "druggability_note",
)


# --- the blinded dossier ----------------------------------------------------------------


def _facet(value: Any) -> str:
    """A structured facet as text: scalars of a dict (identity keys dropped), list items
    joined; empty values give ``""``."""
    if value is None:
        return ""
    if isinstance(value, dict):
        parts = [
            f"{key}: {item}" for key, item in sorted(value.items())
            if key not in _IDENTITY_KEYS and isinstance(item, str | int | float | bool)
            and item != ""
        ]
        return ", ".join(parts)
    if isinstance(value, list | tuple):
        return "; ".join(text for text in (_facet(item) for item in value) if text)
    return str(value)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _idea_section(idea: dict, documents: list[dict]) -> list[str]:
    lines = ["# Idea"]
    for label, key in (
        ("Type", "idea_type"), ("Targets", "targets"), ("Direction", "direction"),
        ("Agent", "agent"), ("Disease", "disease"), ("Modality variants", "modality_variants"),
        ("Stage", "stage"), ("Evidence grade", "evidence_grade"),
    ):
        lines.append(f"- {label}: {_facet(idea.get(key)) or 'none given'}")
    lines.append(f"- Independent JHU groups: {idea.get('jhu_group_count') or 0}")
    total = idea.get("document_count") or len(documents)
    lines.append(f"- Documents: {total} in all, {len(documents)} listed")
    return lines


def _evidence_section(evidence: dict) -> list[str]:
    lines = ["", "# Database evidence"]
    summary = evidence.get("evidence_summary")
    lines.append(_json(summary) if summary else "No database evidence was found.")
    status = evidence.get("evidence_status") or {}
    unavailable = sorted(
        source for source, entry in status.items()
        if isinstance(entry, dict) and entry.get("status") == "failed"
    )
    if unavailable:
        lines.append(
            "Unavailable (lookup failed; unknown, not a negative finding): "
            + ", ".join(unavailable)
        )
    lines += ["", "## Data flags (never exclude an idea)"]
    flags = evidence.get("data_flags") or {}
    lines += [f"- {key}: {_json(flags[key])}" for key in sorted(flags)] or ["- none computed"]
    return lines


def _documents_section(documents: list[dict]) -> list[str]:
    lines = ["", "# Documents"]
    for n, doc in enumerate(documents, start=1):
        kind = doc.get("kind") or "unknown"
        if doc.get("document_type"):
            kind = f"{kind} ({doc['document_type']})"
        lines.append(f"{n}. {kind}, {doc.get('date') or 'undated'}")
    return lines


def _candidate_text(n: int, candidate: dict) -> list[str]:
    lines = [f"Extract {n}:"]
    if candidate.get("evidence_summary"):
        lines.append(f"Evidence summary: {candidate['evidence_summary']}")
    if candidate.get("mechanism_hypothesis"):
        lines.append(f"Mechanism: {candidate['mechanism_hypothesis']}")
    quoted = [
        e for e in candidate.get("target_rubric") or () if isinstance(e, dict) and e.get("quote")
    ]
    if quoted:
        lines.append("Checklist quotes (verified):")
        for entry in quoted:
            grade = f", grade {entry['grade']}" if entry.get("grade") else ""
            lines.append(
                f"- item {entry.get('item')} ({entry.get('status')}{grade}), "
                f"{entry.get('section') or 'text'}: \"{entry['quote']}\""
            )
    results = [r for r in candidate.get("key_results") or () if isinstance(r, dict)]
    if results:
        lines.append("Key results:")
        lines += [
            f"- {r.get('claim', '')}: \"{r.get('quote', '')}\" ({r.get('section') or 'text'})"
            for r in results
        ]
    systems = [s for s in candidate.get("model_systems") or () if isinstance(s, str) and s]
    if systems:
        lines.append(f"Model systems: {'; '.join(systems)}")
    return lines


def triage_dossier(idea_fields: dict, evidence: dict, documents: list[dict]) -> str:
    """The blinded S4 user message (spec §7.5, D20); pure.

    - ``idea_fields``: ``idea_type``, ``targets``, ``direction``, ``agent``, ``disease``,
      ``modality_variants``, ``stage``, ``evidence_grade``, ``jhu_group_count``, optional
      ``document_count`` (all documents, before the D35 cap) and ``candidates``: the S3
      candidates (extract output shape, quotes already checked) of the listed documents.
    - ``evidence``: ``evidence_summary``, ``data_flags`` and ``evidence_status``; a source
      whose status is ``failed`` is listed as unavailable.
    - ``documents``: one dict per listed document with ``kind``, ``document_type`` and
      ``date`` only.

    Only those fields are read, so no author, lab, institution, journal or grant-holder name
    reaches the message; identity keys inside facet dicts are dropped. The extracted text is
    fenced with ``delimit(..., "publication_abstract")`` (D44).
    """
    lines = _idea_section(idea_fields, documents)
    lines += _evidence_section(evidence)
    lines += _documents_section(documents)
    candidates = [c for c in idea_fields.get("candidates") or () if isinstance(c, dict)]
    lines += ["", "# Extracted evidence"]
    if candidates:
        body: list[str] = []
        for n, candidate in enumerate(candidates, start=1):
            body += [*_candidate_text(n, candidate), ""]
        lines.append(delimit("\n".join(body).rstrip(), "publication_abstract"))
    else:
        lines.append("No extracted evidence.")
    return "\n".join(lines)


# --- requests -----------------------------------------------------------------------------


def _request(dossier: str, *, inputs: EpochInputs, retry: bool) -> dict:
    """The S4 request for ``dossier`` in the epoch's own prompt, model and rubric (§7.11)."""
    return build_request(
        STAGE, model=inputs.model(STAGE), system=inputs.system(STAGE), user=dossier,
        schema=stage_schema(STAGE, inputs.rubric), retry=retry,
    )


# --- submission -----------------------------------------------------------------------------


def _assembly_ended(status: dict | None) -> bool:
    """Evidence assembly has ended once every tracked source is done or failed (§7.4 step 5)."""
    return bool(status) and all(
        isinstance(entry, dict) and entry.get("status") in _ASSEMBLY_ENDED
        for entry in status.values()
    )


def _document_dict(doc: LandscapeDocument) -> dict:
    return {
        "kind": doc.kind,
        "document_type": doc.document_type,
        "date": doc.published_on.isoformat() if doc.published_on else None,
    }


async def _sources(
    db: AsyncSession, idea_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[tuple[LandscapeDocument, dict | None]]]:
    """Per idea, each attached candidate's document (tombstones left out) and candidate."""
    rows = (
        await db.execute(
            select(
                LandscapeIdeaEvidence.idea_id, LandscapeIdeaEvidence.candidate_index,
                LandscapeExtraction.raw, LandscapeDocument,
            )
            .join(
                LandscapeExtraction,
                LandscapeExtraction.id == LandscapeIdeaEvidence.extraction_id,
            )
            .join(LandscapeDocument, LandscapeDocument.id == LandscapeExtraction.document_id)
            .where(
                LandscapeIdeaEvidence.idea_id.in_(idea_ids),
                LandscapeDocument.merged_into.is_(None),
            )
            .order_by(LandscapeIdeaEvidence.idea_id, LandscapeIdeaEvidence.candidate_index)
        )
    ).all()
    out: dict[uuid.UUID, list[tuple[LandscapeDocument, dict | None]]] = defaultdict(list)
    for idea_id, index, raw, doc in rows:
        ideas = (raw or {}).get("ideas") or []
        candidate = ideas[index] if 0 <= index < len(ideas) else None
        out[idea_id].append((doc, candidate if isinstance(candidate, dict) else None))
    return out


def _dossier_for(
    idea: LandscapeIdea, sources: list[tuple[LandscapeDocument, dict | None]]
) -> str:
    unique = list({doc.id: doc for doc, _ in sources}.values())
    listed = dossier_documents(idea, unique)
    listed_ids = [doc.id for doc in listed]
    candidates = [
        candidate for doc_id in listed_ids
        for doc, candidate in sources if doc.id == doc_id and candidate is not None
    ]
    idea_fields = {
        "idea_type": idea.idea_type, "targets": idea.targets, "direction": idea.direction,
        "agent": idea.agent, "disease": idea.disease,
        "modality_variants": idea.modality_variants, "stage": idea.stage,
        "evidence_grade": idea.evidence_grade, "jhu_group_count": idea.jhu_group_count,
        "document_count": len(unique), "candidates": candidates,
    }
    evidence = {
        "evidence_summary": idea.evidence_summary, "data_flags": idea.data_flags,
        "evidence_status": idea.evidence_status,
    }
    return triage_dossier(idea_fields, evidence, [_document_dict(doc) for doc in listed])


async def _due_rows(
    db: AsyncSession, epoch_id: uuid.UUID
) -> list[tuple[LandscapeTriage, LandscapeIdea]]:
    """Creates a ``pending`` row for each idea version that is due and has none, and returns
    every ``pending`` row of a current version that is not waiting for the fallback chain."""
    t = LandscapeTriage
    fresh = (
        await db.scalars(
            select(LandscapeIdea).where(
                LandscapeIdea.epoch_id == epoch_id,
                LandscapeIdea.version_hash.is_not(None),
                LandscapeIdea.evidence_status.is_not(None),
                ~exists().where(
                    t.idea_id == LandscapeIdea.id, t.epoch_id == epoch_id,
                    t.idea_version == LandscapeIdea.version_hash,
                ),
            )
        )
    ).all()
    for idea in fresh:
        if _assembly_ended(idea.evidence_status):
            db.add(LandscapeTriage(
                idea_id=idea.id, idea_version=idea.version_hash, epoch_id=epoch_id,
            ))
    await db.flush()
    return list(
        (
            await db.execute(
                select(t, LandscapeIdea)
                .join(LandscapeIdea, LandscapeIdea.id == t.idea_id)
                .where(
                    t.epoch_id == epoch_id, t.status == "pending", t.fallback_from.is_(None),
                    t.idea_version == LandscapeIdea.version_hash,
                )
                .order_by(t.created_at, t.id)
            )
        ).tuples().all()
    )


async def submit_triage_batch(db_factory, epoch_id) -> int:
    """Submit one S4 batch for ``epoch_id``; returns the number of items sent (0: nothing due).

    Due: each idea whose current ``version_hash`` has no triage row in the epoch and whose
    evidence assembly has ended (a ``failed`` source is marked unavailable, so it never
    blocks triage), plus every ``pending`` row of a current version left by a resubmission.
    The dossier of each row is stored in ``raw`` and committed before ``batches.submit``.
    """
    async with db_factory() as db:
        due = await _due_rows(db, epoch_id)
        if not due:
            await db.commit()
            return 0
        sources: dict[uuid.UUID, list] = {}
        idea_ids = [idea.id for _, idea in due]
        for start in range(0, len(idea_ids), _QUERY_CHUNK):
            sources.update(await _sources(db, idea_ids[start:start + _QUERY_CHUNK]))
        inputs = await epochs.epoch_inputs(db, epoch_id)
        items: list[tuple[uuid.UUID, dict]] = []
        for row, idea in due:
            dossier = _dossier_for(idea, sources.get(idea.id, []))
            row.raw = {"dossier": dossier}
            items.append((row.id, _request(dossier, inputs=inputs, retry=row.truncated_once)))
        await db.commit()
    try:
        await batches.submit(db_factory, stage=STAGE, row_model=LandscapeTriage, items=items)
    except ValueError:
        logger.info("triage: no row still pending at submission for epoch %s", epoch_id)
        return 0
    return len(items)


# --- results ----------------------------------------------------------------------------


def _result_values(parsed: Any, inputs: EpochInputs) -> dict:
    """Stage-row values for a parsed triage result, scored with the epoch's rubric and
    criteria; ``ValueError`` when the three scores are not integers on the rubric's scale."""
    if not isinstance(parsed, dict):
        raise ValueError("triage output is not a JSON object")
    rubric = inputs.rubric
    raw_scores = parsed.get("scores") or {}
    scores: dict[str, int] = {}
    for key in TRIAGE_DIMENSIONS:
        value = raw_scores.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not (
            rubric.scale_min <= value <= rubric.scale_max
        ):
            raise ValueError(f"triage score {key!r} is not an integer on the scale: {value!r}")
        scores[key] = value
    target_rubric = parsed.get("target_rubric") or []
    reason = excluded_reason(parsed)
    values = {field: parsed.get(field) for field in _STORED_FIELDS}
    values.update(
        status=reason or "done",
        scores=scores,
        triage_score=triage_score(
            scores, target_rubric, rubric=rubric, criteria=inputs.criteria
        ),
        evidence_count=present_items(target_rubric, rubric=rubric),
        exclusion_reason=_exclusion_text(parsed, reason),
    )
    return values


def _exclusion_text(parsed: dict, reason: str | None) -> str | None:
    if reason == "excluded:gate":
        failed = sorted(k for k, v in (parsed.get("gating") or {}).items() if v == "not_met")
        return "gate not met: " + ", ".join(failed)
    if reason == "excluded:scope":
        return "outside the therapeutic scope"
    return None


def _message_text(message: Any) -> str:
    return "".join(
        block.text for block in (getattr(message, "content", None) or [])
        if getattr(block, "type", None) == "text"
    )


def _parsed_values(text: str, raw: dict | None, inputs: EpochInputs) -> dict:
    """The row values for a reply text: the scored result, or ``failed:unparseable`` (terminal)
    with the error stored."""
    try:
        parsed = extract_json(text)
        values = _result_values(parsed, inputs)
    except ValueError as exc:
        return {"status": "failed:unparseable", "error": job_error_text(exc)}
    values["raw"] = {**(raw or {}), "output": parsed}
    return values


async def ingest_triage_outcomes(db_factory, outcomes: list[BatchOutcome]) -> None:
    """Apply one chunk of S4 outcomes through ``batches.apply_result`` (compare-and-set), so a
    replayed chunk changes nothing. ``succeeded`` stores the result and its computed score
    (``done``, ``excluded:gate`` or ``excluded:scope``); other kinds take
    ``batches.transition``."""
    async with db_factory() as db:
        for outcome in outcomes:
            row = await db.get(LandscapeTriage, outcome.row_id)
            if row is None:
                continue
            if outcome.kind == "succeeded":
                inputs = await epochs.epoch_inputs(db, row.epoch_id)
                values = _parsed_values(_message_text(outcome.message), row.raw, inputs)
                values["served_by_model"] = outcome.model
            else:
                values = batches.transition(
                    outcome.kind, attempts=row.attempts, truncated_once=row.truncated_once,
                    model=outcome.model,
                )
            await batches.apply_result(
                db, LandscapeTriage, row_id=outcome.row_id,
                batch_item_id=outcome.batch_item_id, values=values,
            )
        await db.commit()


# --- the fallback chain (D42) ---------------------------------------------------------------


async def _fallback_request(db: AsyncSession, row: LandscapeTriage) -> dict:
    """The row's request from its stored dossier, in its epoch's inputs (D42)."""
    dossier = (row.raw or {}).get("dossier")
    if not dossier:
        raise ValueError(f"triage row {row.id} has no stored dossier")
    inputs = await epochs.epoch_inputs(db, row.epoch_id)
    return _request(dossier, inputs=inputs, retry=row.truncated_once)


async def _fallback_success(db: AsyncSession, row: LandscapeTriage, result: CallResult) -> None:
    inputs = await epochs.epoch_inputs(db, row.epoch_id)
    for column, value in _parsed_values(result.text, row.raw, inputs).items():
        setattr(row, column, value)


FALLBACKS: tuple[FallbackAdapter, ...] = (
    FallbackAdapter(STAGE, LandscapeTriage, _fallback_request, _fallback_success),
)
