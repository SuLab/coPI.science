"""S9, the final assessment (batch; spec §7.10, L41, L42, L60, D15, D29, D37, D59, D62).

One ``landscape_assessments`` row per subject, and the row is the stage row:

- **Active epoch:** the latest version of every opportunity that is not retired and whose
  version is tagged with the epoch, probes included. A new version of an opportunity already
  assessed in this batch cycle waits for the next cycle (D37).
- **Candidate epoch:** one per current provisional grouping, keyed ``(epoch_id, grouping_id)``
  (D59), and one per issued probe, keyed by the probe's own ``(opportunity_id,
  opportunity_version)`` with the candidate's ``epoch_id`` (D62).

A row is created only once every member idea has a ``done`` S6 record in the row's epoch and,
for a subject of two or more ideas, its S8 label has ended; S7 is never waited for. Requests are
sent in the order of the best member's S7 position (its S5 order until S7 has placed it), but
neither appears in the request: the message lists members by ``idea_key`` and carries no rank,
position or card order, so a verdict can never reveal rank in a blind batch (spec §7.10, F2).

The system prompt (the final prompt, the verdict-contract extract and the criteria), the rubric
that computes the weighted score and band, and the model are the epoch's own
(``epochs.epoch_inputs``, spec §7.11).

Decisions and reviews are keyed on the opportunity and record the assessment they were made on
(spec §13.3), so a new assessment never touches them: they carry forward and read "changed
since your decision" (L60, D50).
"""
from __future__ import annotations

import json
import math
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.agent.engine.sidecar import _normalize_gating
from src.agent.prompt_safety import delimit
from src.landscaping import batches, epochs
from src.landscaping.batches import BatchOutcome, FallbackAdapter
from src.landscaping.comparator import IN_FLIGHT
from src.landscaping.epochs import EpochInputs
from src.landscaping.ideas import dossier_documents
from src.landscaping.llm_client import CallResult, build_request
from src.landscaping.schemas import stage_schema
from src.landscaping.scoring import epoch_band, epoch_weighted_score
from src.landscaping.stages.cut import _batch_cycle_start
from src.landscaping.stages.screen import message_text, parse_output, served_by, submit_items
from src.landscaping.structured_lines import structured_line
from src.models.landscape import (
    LandscapeAssessment,
    LandscapeCandidateGrouping,
    LandscapeConsult,
    LandscapeCut,
    LandscapeDeep,
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeExtraction,
    LandscapeIdea,
    LandscapeIdeaAlias,
    LandscapeIdeaEvidence,
    LandscapeLab,
    LandscapeOpportunity,
    LandscapeOpportunityVersion,
    LandscapeRanking,
    LandscapeReviewBatchItem,
)
from src.services.assessment_detail import normalize_key_points
from src.services.blackbird_rubric import Rubric
from src.services.job_queue import job_error_text
from src.services.verdict_fields import sidecar_column_kwargs

STAGE = "final"
#: The S9 status of an issued probe that cannot be assessed in a candidate (spec §7.11 step 4).
NO_DEEP = "failed:no_deep"
#: Marks a row whose first reply was invalid; the second invalid reply is ``failed:invalid``
#: (spec §7.10).
_INVALID_ONCE = "invalid reply, retrying once: "
#: ``member_notes`` lines are at most this many characters (final.md).
_NOTE_MAX = 200


# --- subjects and members -------------------------------------------------------------------


@dataclass(frozen=True)
class _Subject:
    """What one assessment row assesses: an opportunity version or a provisional grouping."""

    label: str | None
    theme: str | None
    platform_signal: dict | None
    member_keys: tuple[str, ...]


def _label_in_flight(row: Any) -> bool:
    """A subject of two or more ideas waits for its S8 label; a single idea has no call."""
    return len(row.member_idea_keys or []) >= 2 and row.status in IN_FLIGHT


def _subject_of(row: Any) -> _Subject:
    return _Subject(
        label=row.label, theme=row.theme, platform_signal=row.platform_signal,
        member_keys=tuple(sorted(row.member_idea_keys or [])),
    )


async def _subject(db: AsyncSession, row: LandscapeAssessment) -> _Subject | None:
    if row.grouping_id is not None:
        grouping = await db.get(LandscapeCandidateGrouping, row.grouping_id)
        return _subject_of(grouping) if grouping is not None else None
    V = LandscapeOpportunityVersion
    version = await db.scalar(
        select(V).where(
            V.opportunity_id == row.opportunity_id, V.version == row.opportunity_version
        )
    )
    return _subject_of(version) if version is not None else None


async def _idea_for_key(db: AsyncSession, epoch_id: uuid.UUID, key: str) -> LandscapeIdea | None:
    """The epoch's idea for ``key``, directly or through the epoch's aliases (D36, D64)."""
    idea = await db.scalar(
        select(LandscapeIdea).where(
            LandscapeIdea.epoch_id == epoch_id, LandscapeIdea.idea_key == key
        )
    )
    if idea is not None:
        return idea
    return await db.scalar(
        select(LandscapeIdea)
        .join(LandscapeIdeaAlias, LandscapeIdeaAlias.idea_id == LandscapeIdea.id)
        .where(LandscapeIdeaAlias.epoch_id == epoch_id, LandscapeIdeaAlias.old_key == key)
    )


async def _newest_deep(
    db: AsyncSession, epoch_id: uuid.UUID, idea_id: uuid.UUID, *, done_only: bool
) -> LandscapeDeep | None:
    query = select(LandscapeDeep).where(
        LandscapeDeep.epoch_id == epoch_id, LandscapeDeep.idea_id == idea_id
    )
    if done_only:
        query = query.where(LandscapeDeep.status == "done")
    return await db.scalar(
        query.order_by(LandscapeDeep.created_at.desc(), LandscapeDeep.id.desc()).limit(1)
    )


async def _members(
    db: AsyncSession, epoch_id: uuid.UUID, keys: tuple[str, ...] | list[str]
) -> list[tuple[LandscapeIdea, LandscapeDeep]] | None:
    """Each member idea with its newest ``done`` S6 record in ``epoch_id``, by ``idea_key``;
    None while any member has none (or no idea in the epoch)."""
    out: list[tuple[LandscapeIdea, LandscapeDeep]] = []
    seen: set[uuid.UUID] = set()
    for key in sorted(keys):
        idea = await _idea_for_key(db, epoch_id, key)
        if idea is None:
            return None
        if idea.id in seen:
            continue
        deep = await _newest_deep(db, epoch_id, idea.id, done_only=True)
        if deep is None:
            return None
        seen.add(idea.id)
        out.append((idea, deep))
    return out or None


# --- the user message -------------------------------------------------------------------------


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


async def _documents(db: AsyncSession, idea: LandscapeIdea) -> list[str]:
    """The member's dossier documents (D35), one line each, with their labs."""
    rows = (
        await db.scalars(
            select(LandscapeDocument)
            .join(LandscapeExtraction, LandscapeExtraction.document_id == LandscapeDocument.id)
            .join(
                LandscapeIdeaEvidence,
                LandscapeIdeaEvidence.extraction_id == LandscapeExtraction.id,
            )
            .where(
                LandscapeIdeaEvidence.idea_id == idea.id,
                LandscapeDocument.merged_into.is_(None),
            )
        )
    ).all()
    listed = dossier_documents(idea, list({doc.id: doc for doc in rows}.values()))
    lab_keys = sorted({doc.lab_key for doc in listed if doc.lab_key})
    labs = dict(
        (
            await db.execute(
                select(LandscapeLab.lab_key, LandscapeLab.display_name)
                .where(LandscapeLab.lab_key.in_(lab_keys))
            )
        ).all()
    ) if lab_keys else {}
    lines = []
    for doc in listed:
        kind = f"{doc.kind} ({doc.document_type})" if doc.document_type else doc.kind
        parts = [
            f"id {doc.id}", kind,
            doc.published_on.isoformat() if doc.published_on else "undated",
            f"title: {doc.title or 'untitled'}",
            f"lab: {labs.get(doc.lab_key) or doc.lab_key or 'unknown'}",
        ]
        if doc.qualifying_institution:
            parts.append(f"institution: {doc.qualifying_institution}")
        if doc.role_basis:
            parts.append(f"basis: {doc.role_basis}")
        if doc.dates:
            parts.append(f"dates: {_json(doc.dates)}")
        lines.append("- " + "; ".join(parts))
    return lines


async def _opinions(db: AsyncSession, deep_id: uuid.UUID) -> dict:
    rows = (
        await db.scalars(
            select(LandscapeConsult)
            .where(LandscapeConsult.deep_id == deep_id)
            .order_by(LandscapeConsult.domain)
        )
    ).all()
    return {row.domain: row.opinion if row.opinion is not None else row.raw_text for row in rows}


def _idea_facts(idea: LandscapeIdea) -> dict:
    """The idea's normalized facets and database evidence; never its cut membership."""
    return {
        "idea_type": idea.idea_type, "targets": idea.targets, "direction": idea.direction,
        "agent": idea.agent, "disease": idea.disease,
        "modality_variants": idea.modality_variants, "stage": idea.stage,
        "evidence_grade": idea.evidence_grade, "jhu_group_count": idea.jhu_group_count,
        "outside_group_count": idea.outside_group_count,
        "evidence_summary": idea.evidence_summary, "data_flags": idea.data_flags,
    }


def _deep_facts(deep: LandscapeDeep) -> dict:
    """The S6 record the assessment reads (spec §7.10 Inputs), full-text evidence apart."""
    return {
        "summary": deep.summary, "disease_profile": deep.disease_profile,
        "target_biology": deep.target_biology, "gating": deep.gating,
        "gating_rationales": deep.gating_rationales, "flags_reviewed": deep.flags_reviewed,
        "provisional_scores": deep.provisional_scores,
        "dimension_rationales": deep.dimension_rationales,
        "outside_group_count": deep.outside_group_count,
        "retrieved_sources": deep.search_transcript,
    }


async def _member_section(db: AsyncSession, idea: LandscapeIdea, deep: LandscapeDeep) -> str:
    """One member's section. Source-derived text is fenced (D44): the record as
    ``publication_abstract``, the full-text evidence as ``methods_section``."""
    documents = await _documents(db, idea)
    record = "\n".join([
        "## Idea", _json(_idea_facts(idea)),
        "", "## Documents", *(documents or ["No documents listed."]),
        "", "## Deep analysis", _json(_deep_facts(deep)),
        "", "## Specialist opinions", _json(await _opinions(db, deep.id)),
    ])
    full_text = _json(deep.full_text_evidence) if deep.full_text_evidence else "None."
    return "\n".join([
        f"# Member {idea.idea_key}",
        "Structured line:",
        # Built from S3's as-written names, model output copied from source text: fenced too.
        delimit(structured_line(idea), "publication_abstract"),
        delimit(record, "publication_abstract"),
        "Full-text evidence:",
        delimit(full_text, "methods_section"),
    ])


async def _message(
    db: AsyncSession, subject: _Subject, members: list[tuple[LandscapeIdea, LandscapeDeep]]
) -> str:
    """The S9 user message: the opportunity's label, theme and platform signal, then every
    member in ``idea_key`` order. No rank, position or card order (F2)."""
    lines = [
        "# Opportunity",
        f"Label: {subject.label or 'none; use the structured lines'}",
        f"Theme: {subject.theme or 'none'}",
        f"Platform signal: {_json(subject.platform_signal) if subject.platform_signal else 'none'}",
        f"Member ideas: {len(members)}",
    ]
    sections = [await _member_section(db, idea, deep) for idea, deep in members]
    return "\n\n".join(["\n".join(lines), *sections])


async def _message_for(db: AsyncSession, row: LandscapeAssessment) -> str | None:
    subject = await _subject(db, row)
    if subject is None:
        return None
    members = await _members(db, row.epoch_id, subject.member_keys)
    if members is None:
        return None
    return await _message(db, subject, members)


def _request(message: str, *, inputs: EpochInputs, retry: bool) -> dict:
    """The S9 request in the epoch's own prompt, model and rubric (§7.11)."""
    return build_request(
        STAGE, model=inputs.model(STAGE), system=inputs.system(STAGE), user=message,
        schema=stage_schema(STAGE, inputs.rubric), retry=retry,
    )


# --- which subjects are due -----------------------------------------------------------------


def _new_row(epoch_id: uuid.UUID, **values: Any) -> dict:
    """An assessment row for a multi-row insert; every row carries the same keys."""
    return {
        "id": uuid.uuid4(), "epoch_id": epoch_id, "opportunity_id": None,
        "opportunity_version": None, "grouping_id": None, "status": "pending", "error": None,
        **values,
    }


async def _active_due(db: AsyncSession, epoch_id: uuid.UUID) -> list[dict]:
    """New rows for the epoch's latest opportunity versions with no assessment in it. An
    opportunity with an assessment made since the batch cycle began waits (D37)."""
    V, Opp, A = LandscapeOpportunityVersion, LandscapeOpportunity, LandscapeAssessment
    later = aliased(V)
    latest = select(func.max(later.version)).where(
        later.opportunity_id == V.opportunity_id
    ).scalar_subquery()
    has_row = exists().where(
        A.opportunity_id == V.opportunity_id, A.opportunity_version == V.version,
        A.epoch_id == epoch_id,
    )
    versions = (
        await db.scalars(
            select(V).join(Opp, Opp.id == V.opportunity_id).where(
                V.epoch_id == epoch_id, V.version == latest, Opp.retired_at.is_(None),
                ~has_row,
            )
        )
    ).all()
    if not versions:
        return []
    cycle_start = await _batch_cycle_start(db)
    recent = set(
        (
            await db.scalars(
                select(A.opportunity_id).where(
                    A.opportunity_id.in_([v.opportunity_id for v in versions]),
                    A.created_at >= cycle_start,
                )
            )
        ).all()
    )
    rows = []
    for version in versions:
        if version.opportunity_id in recent or _label_in_flight(version):
            continue
        if await _members(db, epoch_id, version.member_idea_keys) is None:
            continue
        rows.append(_new_row(
            epoch_id, opportunity_id=version.opportunity_id, opportunity_version=version.version,
        ))
    return rows


async def _issued_probes(db: AsyncSession) -> list[tuple[uuid.UUID, int, str]]:
    """``(opportunity_id, version, idea_key)`` of every probe issued in a review batch, at its
    latest probe version."""
    V = LandscapeOpportunityVersion
    issued = select(LandscapeReviewBatchItem.opportunity_id).where(
        LandscapeReviewBatchItem.is_probe.is_(True)
    )
    rows = (
        await db.execute(
            select(V.opportunity_id, V.version, V.member_idea_keys)
            .where(V.is_probe.is_(True), V.opportunity_id.in_(issued))
            .order_by(V.opportunity_id, V.version)
        )
    ).all()
    latest: dict[uuid.UUID, tuple[uuid.UUID, int, str]] = {}
    for opportunity_id, version, keys in rows:
        if keys:
            latest[opportunity_id] = (opportunity_id, version, keys[0])
    return list(latest.values())


async def _probe_rows(db: AsyncSession, epoch_id: uuid.UUID) -> dict[tuple, str]:
    """``(opportunity_id, opportunity_version) -> status`` of the epoch's probe-keyed rows."""
    A = LandscapeAssessment
    rows = (
        await db.execute(
            select(A.opportunity_id, A.opportunity_version, A.status)
            .where(A.epoch_id == epoch_id, A.opportunity_id.is_not(None))
        )
    ).all()
    return {(opp, version): status for opp, version, status in rows}


async def _candidate_due(db: AsyncSession, epoch_id: uuid.UUID) -> list[dict]:
    """New rows for the candidate's current groupings (D59) and issued probes (D62)."""
    G, A = LandscapeCandidateGrouping, LandscapeAssessment
    groupings = (
        await db.scalars(
            select(G).where(
                G.epoch_id == epoch_id, G.current.is_(True),
                ~exists().where(A.epoch_id == epoch_id, A.grouping_id == G.id),
            )
        )
    ).all()
    rows = []
    for grouping in groupings:
        if _label_in_flight(grouping):
            continue
        if await _members(db, epoch_id, grouping.member_idea_keys) is not None:
            rows.append(_new_row(epoch_id, grouping_id=grouping.id))
    existing = await _probe_rows(db, epoch_id)
    for opportunity_id, version, key in await _issued_probes(db):
        if (opportunity_id, version) in existing:
            continue
        if await _members(db, epoch_id, [key]) is not None:
            rows.append(_new_row(
                epoch_id, opportunity_id=opportunity_id, opportunity_version=version,
            ))
    return rows


async def _order_keys(db: AsyncSession, epoch_id: uuid.UUID) -> tuple[dict, dict]:
    """The epoch's S7 positions by idea id, and its current cut's S5 order by idea id string."""
    positions = dict(
        (
            await db.execute(
                select(LandscapeRanking.idea_id, LandscapeRanking.position)
                .where(LandscapeRanking.epoch_id == epoch_id)
            )
        ).all()
    )
    order = await db.scalar(
        select(LandscapeCut.ordered_idea_ids)
        .where(LandscapeCut.epoch_id == epoch_id, LandscapeCut.status == "complete")
        .order_by(LandscapeCut.completed_at.desc().nulls_last())
        .limit(1)
    )
    return positions, {idea_id: n for n, idea_id in enumerate(order or [])}


def _best(ideas: list[LandscapeIdea], positions: dict, cut_order: dict) -> tuple[int, int]:
    """The submission order of a subject: its best member's S7 position, else its S5 order."""
    keys = [
        (0, positions[idea.id]) if idea.id in positions
        else (1, cut_order[str(idea.id)]) if str(idea.id) in cut_order
        else (2, 0)
        for idea in ideas
    ]
    return min(keys, default=(2, 0))


async def submit_final_batch(db_factory, *, epoch_id: uuid.UUID) -> int:
    """Submit one S9 batch for ``epoch_id``; returns the number of requests sent (0: none).

    First creates a ``pending`` row for every subject that is due (the module docstring
    gives the scope per epoch status), then sends every ``pending`` row of the epoch not
    waiting for the fallback chain (D42), resubmissions included, best member first. A row
    whose subject or member S6 record has since gone is set ``failed:no_deep``.
    """
    async with db_factory() as db:
        epoch = await db.get(LandscapeEpoch, epoch_id)
        if epoch is None or epoch.status == "retired":
            return 0
        due = await (_active_due if epoch.status == "active" else _candidate_due)(db, epoch_id)
        if due:
            await db.execute(pg_insert(LandscapeAssessment).values(due).on_conflict_do_nothing())
        await db.commit()
        inputs = await epochs.epoch_inputs(db, epoch_id)
        positions, cut_order = await _order_keys(db, epoch_id)
        A = LandscapeAssessment
        rows = (
            await db.scalars(
                select(A).where(
                    A.epoch_id == epoch_id, A.status == "pending", A.fallback_from.is_(None),
                ).order_by(A.created_at, A.id)
            )
        ).all()
        ordered: list[tuple[tuple[int, int], uuid.UUID, dict]] = []
        for row in rows:
            subject = await _subject(db, row)
            members = (
                await _members(db, epoch_id, subject.member_keys) if subject else None
            )
            if members is None:
                row.status = NO_DEEP
                row.error = job_error_text("a member idea has no done S6 record in the epoch")
                continue
            request = _request(
                await _message(db, subject, members), inputs=inputs, retry=row.truncated_once,
            )
            ordered.append((_best([i for i, _ in members], positions, cut_order), row.id, request))
        await db.commit()
    ordered.sort(key=lambda item: item[0])
    return await submit_items(
        db_factory, stage=STAGE, row_model=LandscapeAssessment,
        items=[(row_id, request) for _, row_id, request in ordered],
    )


# --- results --------------------------------------------------------------------------------


def _gating(raw: Any, rubric: Rubric) -> dict:
    """Every rubric gate in the tri-state contract (``_normalize_gating``); ``ValueError``
    when any is missing or not tri-state."""
    kept = _normalize_gating(raw) or {}
    missing = [key for key in rubric.gating if key not in kept]
    if missing:
        raise ValueError("gating is not met/not_met/unconfirmed for: " + ", ".join(missing))
    return {key: kept[key] for key in rubric.gating}


def _scores(raw: Any, rubric: Rubric) -> dict:
    """Every rubric dimension's score, clipped to the rubric's scale; ``ValueError`` when one
    is missing or not a finite number."""
    if not isinstance(raw, dict):
        raise ValueError("scores is not an object")
    out: dict[str, int | float] = {}
    for dim in rubric.dimensions:
        value = raw.get(dim.key)
        numeric = isinstance(value, int | float) and not isinstance(value, bool)
        if not numeric or not math.isfinite(value):
            raise ValueError(f"score {dim.key!r} is not a number: {value!r}")
        clipped = min(max(value, rubric.scale_min), rubric.scale_max)
        out[dim.key] = int(clipped) if float(clipped).is_integer() else float(clipped)
    return out


def _texts(raw: Any, keys) -> dict | None:
    """``raw``'s non-blank strings under ``keys``; None when there are none."""
    if not isinstance(raw, dict):
        return None
    out = {
        key: raw[key].strip() for key in keys
        if isinstance(raw.get(key), str) and raw[key].strip()
    }
    return out or None


def _strings(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def _support(entry: Any) -> list[str] | str:
    """One bullet's support: its references, or ``"estimate"`` when it gives none (L30)."""
    refs = _strings(entry) if isinstance(entry, list) else []
    return refs or "estimate"


def _claim_support(parsed: dict, key_points: Any) -> dict | None:
    """Per key-point group, one support entry per stored bullet, in bullet order (L30). The
    model's entries are aligned to its bullets before blank bullets are dropped, as
    ``normalize_key_points`` drops them; a bullet given no entry is an ``"estimate"``."""
    if not isinstance(key_points, dict):
        return None
    raw_points = parsed.get("key_points")
    raw_support = parsed.get("claim_support")
    raw_support = raw_support if isinstance(raw_support, dict) else {}
    out: dict[str, list] = {}
    for group in key_points:
        entries = raw_support.get(group)
        entries = entries if isinstance(entries, list) else []
        out[group] = [
            _support(entries[n] if n < len(entries) else None)
            for n, bullet in enumerate(raw_points[group])
            if bullet.strip()
        ]
    return out


def _member_notes(raw: Any) -> list[dict] | None:
    notes = [
        {"idea": item["idea"].strip(), "note": item["note"].strip()[:_NOTE_MAX]}
        for item in (raw if isinstance(raw, list) else [])
        if isinstance(item, dict) and isinstance(item.get("idea"), str)
        and isinstance(item.get("note"), str) and item["note"].strip()
    ]
    return notes or None


def _verdict_values(parsed: Any, rubric: Rubric) -> dict:
    """Assessment columns for a parsed reply (spec §7.10 Validation and Computed).

    ``ValueError`` when the reply is not an object, a gate is not tri-state or a score is
    missing. The contract's plain fields go through ``sidecar_column_kwargs`` (bounded ones
    clipped); the weighted score and band from ``rubric`` (``scoring.epoch_weighted_score``,
    ``scoring.epoch_band``), never the model's;
    ``raw_verdict`` keeps the reply as given.
    """
    if not isinstance(parsed, dict):
        raise ValueError("the reply is not a JSON object")
    gating = _gating(parsed.get("gating"), rubric)
    scores = _scores(parsed.get("scores"), rubric)
    key_points = normalize_key_points(parsed.get("key_points"))
    score = epoch_weighted_score(rubric, scores)
    values = sidecar_column_kwargs(parsed)
    values.update(
        gating=gating,
        gating_rationales=_texts(parsed.get("gating_rationales"), gating),
        scores=scores,
        dimension_rationales=_texts(parsed.get("dimension_rationales"), scores),
        red_flags=_strings(parsed.get("red_flags")),
        key_points=key_points,
        claim_support=_claim_support(parsed, key_points),
        member_notes=_member_notes(parsed.get("member_notes")),
        weighted_score=score,
        band=epoch_band(rubric, score),
        raw_verdict=parsed,
        rubric_version=rubric.version,
        rubric_content_hash=rubric.content_hash,
    )
    return values


def _invalid(row: LandscapeAssessment, reason: str) -> dict:
    """An invalid reply goes back to ``pending`` once, for the same model (the next batch,
    or the fallback chain for a row that holds ``fallback_from``); the second is
    ``failed:invalid`` (spec §7.10)."""
    if (row.error or "").startswith(_INVALID_ONCE):
        return {"status": "failed:invalid", "error": job_error_text(reason)}
    return {"status": "pending", "error": job_error_text(_INVALID_ONCE + reason)}


async def _reply_values(db: AsyncSession, row: LandscapeAssessment, text: str) -> dict:
    """Row values for a reply ``text``: the validated verdict (``done``) with its structured
    lines, composed in code from the member ideas (D29), or the invalid-reply transition."""
    inputs = await epochs.epoch_inputs(db, row.epoch_id)
    try:
        values = _verdict_values(parse_output(text), inputs.rubric)
    except ValueError as exc:
        return _invalid(row, str(exc))
    subject = await _subject(db, row)
    members = await _members(db, row.epoch_id, subject.member_keys) if subject else None
    values["structured_lines"] = (
        [structured_line(idea) for idea, _ in members] if members else None
    )
    values.update(status="done", error=None)
    return values


async def ingest_final_outcomes(db_factory, outcomes: list[BatchOutcome]) -> None:
    """Apply S9 batch results, each a compare-and-set in its own transaction (spec §7.1 step
    5), so a replayed chunk changes nothing. ``succeeded`` is validated (``_reply_values``);
    every other kind takes ``batches.transition`` (a refusal leaves the row for
    ``FALLBACKS``). Decisions and reviews are never touched: they stay keyed on the
    opportunity (L60)."""
    for outcome in outcomes:
        async with db_factory() as db:
            row = await db.get(LandscapeAssessment, outcome.row_id)
            if row is None:
                continue
            if outcome.kind == "succeeded":
                values = await _reply_values(db, row, message_text(outcome.message))
                values["served_by_model"] = served_by(outcome)
            else:
                values = batches.transition(
                    outcome.kind, attempts=row.attempts, truncated_once=row.truncated_once,
                    model=outcome.model,
                )
            await batches.apply_result(
                db, LandscapeAssessment, row_id=outcome.row_id,
                batch_item_id=outcome.batch_item_id, values=values,
            )
            await db.commit()


# --- the fallback chain (D42) ---------------------------------------------------------------


async def _fallback_request(db: AsyncSession, row: LandscapeAssessment) -> dict:
    """The row's request rebuilt from its subject and members, in its epoch's inputs."""
    message = await _message_for(db, row)
    if message is None:
        raise LookupError(f"assessment row {row.id}: its subject or a member S6 record is gone")
    inputs = await epochs.epoch_inputs(db, row.epoch_id)
    return _request(message, inputs=inputs, retry=row.truncated_once)


async def _fallback_success(
    db: AsyncSession, row: LandscapeAssessment, result: CallResult
) -> None:
    for column, value in (await _reply_values(db, row, result.text)).items():
        setattr(row, column, value)


FALLBACKS: tuple[FallbackAdapter, ...] = (
    FallbackAdapter(STAGE, LandscapeAssessment, _fallback_request, _fallback_success),
)


# --- candidate readiness (spec §7.11 step 4) ------------------------------------------------


def _s9_ended(status: str | None) -> bool:
    return status is not None and (status in ("done", "refused") or status.startswith("failed:"))


def _deep_terminal(status: str) -> bool:
    """S6 ended without a record S9 can read."""
    return status == "refused" or status.startswith(("failed:", "excluded:"))


async def _post_drain_cut(db: AsyncSession, epoch: LandscapeEpoch) -> LandscapeCut | None:
    """The candidate's complete cut taken after its drain, while no boundary batch is pending;
    it stays frozen until activation (``cut._frozen``)."""
    if epoch.drained_at is None:
        return None
    C = LandscapeCut
    pending = exists().where(C.epoch_id == epoch.id, C.status == "boundary_pending")
    if await db.scalar(select(pending)):
        return None
    return await db.scalar(
        select(C)
        .where(C.epoch_id == epoch.id, C.status == "complete", C.created_at > epoch.drained_at)
        .order_by(C.completed_at.desc().nulls_last(), C.created_at.desc())
        .limit(1)
    )


async def _s6_and_s8_done(db: AsyncSession, epoch_id: uuid.UUID, cut: LandscapeCut) -> bool:
    """S6 has ended for every idea in ``cut``, every idea it passed is a member of a current
    grouping recorded with ``cut``, and those groupings' label batch has ended."""
    top = [uuid.UUID(idea_id) for idea_id in (cut.ordered_idea_ids or [])[:cut.k]]
    statuses: dict[uuid.UUID, list[str]] = defaultdict(list)
    if top:
        for idea_id, status in (
            await db.execute(
                select(LandscapeDeep.idea_id, LandscapeDeep.status)
                .where(LandscapeDeep.epoch_id == epoch_id, LandscapeDeep.idea_id.in_(top))
            )
        ).all():
            statuses[idea_id].append(status)
    if any(not statuses[i] or any(s in IN_FLIGHT for s in statuses[i]) for i in top):
        return False
    passed = [i for i in top if "done" in statuses[i]]
    keys = set(
        (await db.scalars(select(LandscapeIdea.idea_key).where(LandscapeIdea.id.in_(passed))))
        .all()
    ) if passed else set()
    G = LandscapeCandidateGrouping
    groupings = (
        await db.scalars(
            select(G).where(G.epoch_id == epoch_id, G.current.is_(True), G.cut_id == cut.id)
        )
    ).all()
    covered = {key for grouping in groupings for key in grouping.member_idea_keys or []}
    return keys <= covered and not any(_label_in_flight(g) for g in groupings)


async def mark_unassessable_probes(db: AsyncSession, *, epoch_id: uuid.UUID) -> int:
    """Give ``failed:no_deep`` to each issued probe with no S9 row in the candidate whose
    candidate S6 ended terminally (refused, ``failed:*`` or ``excluded:*``) or whose idea has
    no row in the candidate (spec §7.11 step 4, D59). Does nothing before the drain, while an
    absent idea may still be on its way through S2-S4. Commits; returns the number marked."""
    epoch = await db.get(LandscapeEpoch, epoch_id, populate_existing=True)
    if epoch is None or epoch.drained_at is None:
        return 0
    existing = await _probe_rows(db, epoch_id)
    marked = []
    for opportunity_id, version, key in await _issued_probes(db):
        if (opportunity_id, version) in existing:
            continue
        idea = await _idea_for_key(db, epoch_id, key)
        if idea is not None:
            deep = await _newest_deep(db, epoch_id, idea.id, done_only=False)
            if deep is None or not _deep_terminal(deep.status):
                continue
            reason = f"the probe idea's S6 ended {deep.status} in this epoch"
        else:
            reason = "the probe idea has no row in this epoch"
        marked.append(_new_row(
            epoch_id, opportunity_id=opportunity_id, opportunity_version=version,
            status=NO_DEEP, error=job_error_text(reason),
        ))
    if marked:
        await db.execute(pg_insert(LandscapeAssessment).values(marked).on_conflict_do_nothing())
        await db.commit()
    return len(marked)


async def candidate_s9_complete(db: AsyncSession, *, epoch_id: uuid.UUID) -> bool:
    """S9 has ended (``done``, ``failed:*`` or ``refused``) for every current grouping of the
    candidate's post-drain cut and for every issued probe (spec §7.11 step 4)."""
    epoch = await db.get(LandscapeEpoch, epoch_id, populate_existing=True)
    cut = await _post_drain_cut(db, epoch) if epoch is not None else None
    if cut is None:
        return False
    G, A = LandscapeCandidateGrouping, LandscapeAssessment
    grouping_ids = (
        await db.scalars(
            select(G.id).where(G.epoch_id == epoch_id, G.current.is_(True), G.cut_id == cut.id)
        )
    ).all()
    by_grouping = dict(
        (
            await db.execute(
                select(A.grouping_id, A.status)
                .where(A.epoch_id == epoch_id, A.grouping_id.in_(grouping_ids))
            )
        ).all()
    ) if grouping_ids else {}
    if not all(_s9_ended(by_grouping.get(gid)) for gid in grouping_ids):
        return False
    by_probe = await _probe_rows(db, epoch_id)
    return all(
        _s9_ended(by_probe.get((opportunity_id, version)))
        for opportunity_id, version, _ in await _issued_probes(db)
    )


async def candidate_ready(db: AsyncSession, *, epoch_id: uuid.UUID) -> bool:
    """Whether the candidate may activate (spec §7.11 step 4), checked in order: it is a
    ``candidate`` with ``drained_at`` set; its post-drain cut exists (complete, taken after
    ``drained_at``, no boundary batch pending); S6 has ended for every idea in that cut and S8
    has grouped it, its label batch ended; then ``mark_unassessable_probes`` (which commits);
    then ``candidate_s9_complete``. The phase rule is ``drained_at``'s: it is set only in
    phase ``continuous``."""
    epoch = await db.get(LandscapeEpoch, epoch_id, populate_existing=True)
    if epoch is None or epoch.status != "candidate" or epoch.drained_at is None:
        return False
    cut = await _post_drain_cut(db, epoch)
    if cut is None or not await _s6_and_s8_done(db, epoch_id, cut):
        return False
    await mark_unassessable_probes(db, epoch_id=epoch_id)
    return await candidate_s9_complete(db, epoch_id=epoch_id)
