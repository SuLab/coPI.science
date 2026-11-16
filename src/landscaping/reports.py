"""Pipeline metrics and reports (spec 2026-10-30 §5.1 "Drift", §10.2, §10.3, §10.6; L32,
D63, D68).

Everything here reads the database; only `pilot_report` writes (one ``landscape_reports``
row). The returned dicts hold only JSON values (ids and times as strings, money as floats),
so the pipeline page renders them as they are and the pilot report stores them unchanged.

**The report epoch.** Stage counts, rates and the positive-control trace describe one epoch:
the active one, else (before the first activation, as in the pilot) the newest candidate
or held one.
"""
from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.landscaping import intake
from src.landscaping.eval import bakeoff
from src.landscaping.institutions import BALTIMORE_OPENALEX_IDS, EXCLUDED_OPENALEX_IDS
from src.landscaping.labs import COLLISION_PREFIX
from src.landscaping.llm_client import STAGE_SPECS
from src.models import AppSetting
from src.models.landscape import (
    LandscapeAssessment,
    LandscapeCandidateGrouping,
    LandscapeComparison,
    LandscapeCut,
    LandscapeDecision,
    LandscapeDeep,
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeExpertJudgment,
    LandscapeExtraction,
    LandscapeFetch,
    LandscapeIdea,
    LandscapeIdeaEvidence,
    LandscapeLab,
    LandscapeLlmCall,
    LandscapeOpportunityVersion,
    LandscapeRegressionRun,
    LandscapeRejectAudit,
    LandscapeReport,
    LandscapeReviewBatch,
    LandscapeReviewBatchItem,
    LandscapeReviewEvent,
    LandscapeScreen,
    LandscapeSource,
    LandscapeTriage,
)
from src.models.user import USER_ROLE_MANAGER, User

#: The epoch-keyed stage rows counted by status, by the name the metrics use.
_STAGE_MODELS = (
    ("screen", LandscapeScreen),
    ("extract", LandscapeExtraction),
    ("triage", LandscapeTriage),
    ("deep", LandscapeDeep),
    ("comparisons", LandscapeComparison),
    ("groupings", LandscapeCandidateGrouping),
    ("opportunities", LandscapeOpportunityVersion),
    ("final", LandscapeAssessment),
    ("audit", LandscapeRejectAudit),
)
#: The document stages whose refusals are broken down by OpenAlex topic.
_DOCUMENT_STAGES = (("screen", LandscapeScreen), ("extract", LandscapeExtraction))
#: The ledger stages whose truncation the pilot reports (S6, its full-text re-extraction, S9).
_TRUNCATION_STAGES = ("deep", "deep_extract", "final")
#: Statuses a positive control passes a stage with.
_PASSED = frozenset({"done", "rescued"})
#: An OpenAlex institution the drift listing considers Hopkins (§5.1 "Drift").
_HOPKINS_NAME = re.compile(r"hopkins|kennedy[\s-]+krieger", re.IGNORECASE)
_LINEAGE_ROOTS = frozenset({"I145311948", "I2799853436"})
#: Expert-agreement groups (D63): items in a batch issued before the draw, and items judged by
#: manager-experts, may have been judged with S9's calls seen, so they are kept apart.
_EXPERT_GROUPS = ("independent", "issued_before_draw", "manager_experts")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _rate(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def _bare(openalex_id: object) -> str:
    return str(openalex_id or "").rstrip("/").rsplit("/", 1)[-1]


async def _report_epoch(db: AsyncSession) -> LandscapeEpoch | None:
    """The active epoch, else the newest candidate or held one, else None."""
    return (await db.execute(
        select(LandscapeEpoch).where(LandscapeEpoch.status.in_(("active", "candidate", "held")))
        .order_by((LandscapeEpoch.status == "active").desc(), LandscapeEpoch.created_at.desc())
        .limit(1)
    )).scalars().first()


async def _current_cut(db: AsyncSession, epoch_id: uuid.UUID) -> LandscapeCut | None:
    """The epoch's newest ``complete`` cut (``stages.cut``'s definition of current)."""
    return (await db.execute(
        select(LandscapeCut).where(
            LandscapeCut.epoch_id == epoch_id, LandscapeCut.status == "complete"
        ).order_by(LandscapeCut.completed_at.desc().nulls_last(), LandscapeCut.created_at.desc())
        .limit(1)
    )).scalars().first()


# --- counts ----------------------------------------------------------------------------------


def _document_source():
    """The source a document is keyed by: OpenAlex when it carries an OpenAlex id, else PubMed
    when it carries a PMID, else the source of its own id. Each document counts once."""
    D = LandscapeDocument
    ids = D.external_ids
    return case(
        (D.openalex_id.is_not(None), "openalex"),
        (D.pmid.is_not(None), "pubmed"),
        (ids.has_key("appl_id") | ids.has_key("core_project"), "reporter"),
        (ids.has_key("nsf_award"), "nsf"),
        (ids.has_key("patent_application"), "odp"),
        (ids.has_key("jhtv_case"), "jhtv"),
        (ids.has_key("datacite_doi"), "datacite"),
        (ids.has_key("geo_accession"), "geo"),
        else_="other",
    )


async def _grouped(db: AsyncSession, column) -> dict[str, int]:
    """Live (non-tombstone) documents counted by `column`; NULL counts as ``unknown``."""
    rows = (await db.execute(
        select(column, func.count()).select_from(LandscapeDocument)
        .where(LandscapeDocument.merged_into.is_(None)).group_by(text("1"))
    )).all()
    return {str(key) if key is not None else "unknown": n for key, n in rows}


async def _stage_counts(db: AsyncSession, epoch_id: uuid.UUID | None) -> dict[str, dict]:
    """``{stage: {status: count}}``: S1 over every live document, the rest in the epoch."""
    counts = {"s1": await _grouped(db, LandscapeDocument.s1_status)}
    if epoch_id is None:
        return counts
    for name, model in _STAGE_MODELS:
        rows = (await db.execute(
            select(model.status, func.count()).where(model.epoch_id == epoch_id)
            .group_by(model.status)
        )).all()
        counts[name] = {status: n for status, n in rows}
    return counts


# --- refusals, spend, cost -------------------------------------------------------------------


async def _refusals_by_stage(db: AsyncSession) -> dict[str, dict]:
    """Per ledger stage: calls, refused calls and their rate (refusals before any fallback,
    since each attempt is its own ledger row)."""
    C = LandscapeLlmCall
    rows = (await db.execute(
        select(C.stage, func.count(), func.count(C.refusal_category)).group_by(C.stage)
    )).all()
    return {
        stage: {"calls": calls, "refused": refused, "rate": _rate(refused, calls)}
        for stage, calls, refused in rows
    }


async def _refusals_by_field(db: AsyncSession, epoch_id: uuid.UUID | None) -> dict[str, dict]:
    """Per document stage and OpenAlex topic: items, items refused at least once (a
    ``refusal_category`` is kept even when a fallback model then served), and the rate. The
    topic is the work's ``primary_topic``, the field OpenAlex records on intake."""
    if epoch_id is None:
        return {}
    topic = LandscapeDocument.details[("extra", "primary_topic")].astext
    out: dict[str, dict] = {}
    for name, model in _DOCUMENT_STAGES:
        rows = (await db.execute(
            select(topic, func.count(), func.count(model.refusal_category)).select_from(model)
            .join(LandscapeDocument, LandscapeDocument.id == model.document_id)
            .where(model.epoch_id == epoch_id).group_by(text("1"))
        )).all()
        out[name] = {
            field or "unknown": {"items": n, "refused": r, "rate": _rate(r, n)}
            for field, n, r in rows
        }
    return out


async def _spend(db: AsyncSession) -> dict[str, dict[str, float]]:
    """``{UTC day: {stage: dollars}}``; unpriced (NULL-cost) calls add nothing."""
    C = LandscapeLlmCall
    day = func.date(func.timezone("UTC", C.created_at))
    rows = (await db.execute(
        select(day, C.stage, func.coalesce(func.sum(C.cost_usd), 0))
        .group_by(text("1"), text("2")).order_by(text("1"))
    )).all()
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for d, stage, cost in rows:
        out[d.isoformat()][stage] = float(cost)
    return dict(out)


async def _total_spend(db: AsyncSession) -> float:
    return float(await db.scalar(
        select(func.coalesce(func.sum(LandscapeLlmCall.cost_usd), 0))
    ))


async def _cost_per_useful(db: AsyncSession) -> dict:
    """Total spend ÷ opportunities a reviewer chose to pursue or approved (L32); the rate is
    None while none is useful."""
    pursued = select(LandscapeDecision.opportunity_id).where(LandscapeDecision.decision == "pursue")
    approved = select(LandscapeReviewEvent.opportunity_id).where(
        LandscapeReviewEvent.action == "approved"
    )
    useful = await db.scalar(
        select(func.count()).select_from(pursued.union(approved).subquery())
    )
    spend = await _total_spend(db)
    return {"spend_usd": spend, "useful": useful, "usd": spend / useful if useful else None}


# --- completeness, quotes, normalization -----------------------------------------------------


async def _completeness(db: AsyncSession) -> dict[str, dict]:
    """Per source: the last slice's completeness ratio, the last success, and how many slices
    stand ``incomplete`` (§12.4)."""
    incomplete = dict((await db.execute(
        select(LandscapeFetch.source, func.count()).where(LandscapeFetch.status == "incomplete")
        .group_by(LandscapeFetch.source)
    )).all())
    out = {}
    for src in (await db.execute(select(LandscapeSource))).scalars():
        out[src.source] = {
            "enabled": src.enabled,
            "last_completeness_ratio": src.last_completeness_ratio,
            "last_success_at": _iso(src.last_success_at),
            "incomplete_slices": incomplete.get(src.source, 0),
        }
    return out


async def _quote_rate(db: AsyncSession, epoch_id: uuid.UUID | None) -> dict:
    if epoch_id is None:
        return {"quotes": 0, "verified": 0, "rate": None}
    E = LandscapeExtraction
    total, verified = (await db.execute(
        select(func.coalesce(func.sum(E.quotes_total), 0),
               func.coalesce(func.sum(E.quotes_verified), 0)).where(E.epoch_id == epoch_id)
    )).one()
    total, verified = int(total), int(verified)
    return {"quotes": total, "verified": verified, "rate": _rate(verified, total)}


def _names(idea_targets, disease, agent) -> list[bool]:
    """One flag per named target, disease and agent: whether it carries its HGNC, Mondo or
    ChEMBL id. Entries with no ``as_written`` name are not names and are skipped."""
    flags = []
    for target in idea_targets or []:
        if isinstance(target, dict) and str(target.get("as_written") or "").strip():
            flags.append(bool(target.get("hgnc_id")))
    for value, id_key in ((disease, "mondo_id"), (agent, "chembl_id")):
        if isinstance(value, dict) and str(value.get("as_written") or "").strip():
            flags.append(bool(value.get(id_key)))
    return flags


async def _normalization(db: AsyncSession, epoch_id: uuid.UUID | None) -> dict:
    """The share of the epoch's idea targets, diseases and agents with an HGNC, Mondo or
    ChEMBL id (§10.2). Normalization never guesses, so every unmatched name is one the
    evidence did not settle on exactly one id: ``ambiguous_names`` counts them."""
    if epoch_id is None:
        return {"names": 0, "matched": 0, "ambiguous_names": 0, "rate": None}
    Idea = LandscapeIdea
    flags: list[bool] = []
    for targets, disease, agent in (await db.execute(
        select(Idea.targets, Idea.disease, Idea.agent).where(Idea.epoch_id == epoch_id)
    )).all():
        flags += _names(targets, disease, agent)
    matched = sum(flags)
    return {"names": len(flags), "matched": matched, "ambiguous_names": len(flags) - matched,
            "rate": _rate(matched, len(flags))}


# --- audit, regression, cut, collisions ------------------------------------------------------


async def _audit(db: AsyncSession, epoch_id: uuid.UUID | None) -> dict:
    """Reject-audit disagreement per audited stage (§10.5), with every disagreement listed."""
    if epoch_id is None:
        return {"by_stage": {}, "disagreements": []}
    A = LandscapeRejectAudit
    rows = (await db.execute(
        select(A).where(A.epoch_id == epoch_id, A.agree.is_not(None)).order_by(A.created_at)
    )).scalars().all()
    by_stage: dict[str, dict] = defaultdict(lambda: {"audited": 0, "disagreed": 0})
    disagreements = []
    for row in rows:
        by_stage[row.item_kind]["audited"] += 1
        if not row.agree:
            by_stage[row.item_kind]["disagreed"] += 1
            disagreements.append({
                "item_kind": row.item_kind, "item_id": str(row.item_id),
                "original": row.original_decision, "audit": row.audit_decision,
                "reason": row.reason,
            })
    for stats in by_stage.values():
        stats["rate"] = _rate(stats["disagreed"], stats["audited"])
    return {"by_stage": dict(by_stage), "disagreements": disagreements}


async def _regression_runs(db: AsyncSession) -> list[dict]:
    """Every regression run, newest first, with the epoch it judged (§10.4)."""
    R = LandscapeRegressionRun
    rows = (await db.execute(select(R).order_by(R.started_at.desc()))).scalars().all()
    return [
        {"epoch_id": str(r.epoch_id), "set_version": r.set_version, "status": r.status,
         "started_at": _iso(r.started_at), "finished_at": _iso(r.finished_at),
         "results": r.results}
        for r in rows
    ]


async def _latest_cut(db: AsyncSession, epoch_id: uuid.UUID | None) -> dict | None:
    cut = await _current_cut(db, epoch_id) if epoch_id is not None else None
    if cut is None:
        return None
    return {"cut_id": str(cut.id), "epoch_id": str(cut.epoch_id), "n": cut.n, "k": cut.k,
            "tie_class_size": cut.tie_class_size, "completed_at": _iso(cut.completed_at)}


async def _collisions(db: AsyncSession) -> list[dict]:
    """Name-key labs left apart because two or more labs carry the name (§5.2): each with the
    lab keys it collided with, recorded by ``labs.unify_name_key``."""
    out = []
    for lab in (await db.execute(
        select(LandscapeLab).where(LandscapeLab.aliases.is_not(None))
        .order_by(LandscapeLab.lab_key)
    )).scalars():
        hits = [a[len(COLLISION_PREFIX):] for a in lab.aliases or []
                if isinstance(a, str) and a.startswith(COLLISION_PREFIX)]
        if hits:
            out.append({"lab_key": lab.lab_key, "display_name": lab.display_name,
                        "collides_with": hits})
    return out


# --- blind comparison and expert agreement ---------------------------------------------------


def _pursue_stats(decisions: list[str]) -> dict:
    pursued = sum(d == "pursue" for d in decisions)
    return {"decisions": len(decisions), "pursued": pursued,
            "pursue_rate": _rate(pursued, len(decisions))}


async def _blind_results(db: AsyncSession) -> list[dict]:
    """The pursue rate of in-cut cards against probes, per blind batch, for superseded
    blind batches only: a blind batch's result, like its probe flags, stays hidden until a
    later batch is issued (spec §10.6, F2)."""
    B, BI = LandscapeReviewBatch, LandscapeReviewBatchItem
    latest = await db.scalar(select(func.max(B.issued_on)))
    if latest is None:
        return []
    batches = (await db.execute(
        select(B).where(B.blind.is_(True), B.issued_on < latest).order_by(B.issued_on)
    )).scalars().all()
    out = []
    for batch in batches:
        rows = (await db.execute(
            select(BI.is_probe, LandscapeDecision.decision).select_from(BI)
            .join(LandscapeDecision, LandscapeDecision.opportunity_id == BI.opportunity_id)
            .where(BI.batch_id == batch.id)
        )).all()
        out.append({
            "batch_id": str(batch.id), "issued_on": batch.issued_on.isoformat(),
            "in_cut": _pursue_stats([d for probe, d in rows if not probe]),
            "probes": _pursue_stats([d for probe, d in rows if probe]),
        })
    return out


def _gates_agree(expert: dict | None, s9: dict | None) -> bool:
    shared = set(expert or {}) & set(s9 or {})
    return bool(shared) and all(expert[k] == s9[k] for k in shared)


def _scores_within_one(expert: dict | None, s9: dict | None) -> bool:
    shared = set(expert or {}) & set(s9 or {})
    try:
        return bool(shared) and all(abs(float(expert[k]) - float(s9[k])) <= 1 for k in shared)
    except (TypeError, ValueError):
        return False


def _expert_group(judgment: LandscapeExpertJudgment, role: str | None) -> str:
    if role == USER_ROLE_MANAGER:
        return "manager_experts"
    if judgment.issued_before_draw:
        return "issued_before_draw"
    return "independent"


async def _expert_agreement(db: AsyncSession) -> dict[str, dict]:
    """Submitted expert judgments compared item by item with the S9 assessment they were drawn
    against (§10.6, L55): recommendation, gates (on the gates both name) and scores within
    ±1, per group (`_EXPERT_GROUPS`), with the recommendation disagreements listed."""
    J, A = LandscapeExpertJudgment, LandscapeAssessment
    rows = (await db.execute(
        select(J, A, User.user_role).select_from(J)
        .join(A, A.id == J.landscape_assessment_id)
        .outerjoin(User, User.id == J.expert_id)
        .where(J.submitted_at.is_not(None)).order_by(J.submitted_at)
    )).all()
    groups = {g: {"items": 0, "recommendation": 0, "gates": 0, "scores": 0,
                  "disagreements": []} for g in _EXPERT_GROUPS}
    for judgment, s9, role in rows:
        stats = groups[_expert_group(judgment, role)]
        stats["items"] += 1
        same = judgment.recommendation == s9.recommendation
        stats["recommendation"] += same
        stats["gates"] += _gates_agree(judgment.gates, s9.gating)
        stats["scores"] += _scores_within_one(judgment.scores, s9.scores)
        if not same:
            stats["disagreements"].append({
                "opportunity_id": str(judgment.opportunity_id), "quarter": judgment.quarter,
                "expert": judgment.expert_name, "expert_recommendation": judgment.recommendation,
                "s9_recommendation": s9.recommendation,
            })
    for stats in groups.values():
        n = stats["items"]
        for key in ("recommendation", "gates", "scores"):
            stats[f"{key}_rate"] = _rate(stats[key], n)
    return groups


# --- public: metrics, drift, positive controls -----------------------------------------------


async def pipeline_metrics(db: AsyncSession) -> dict:
    """The §10.3 running metrics for the pipeline page, from the database alone.

    Keys: ``epoch_id`` (the report epoch, module docstring; None before any epoch), ``counts``
    (``by_stage`` {stage: {status: n}}, ``by_source``, ``by_institution``),
    ``refusals`` (``by_stage`` from the ledger, ``by_field`` for S2/S3), ``spend_by_day``
    {day: {stage: usd}}, ``cost_per_useful_opportunity`` (L32), ``completeness``,
    ``quote_verification``, ``normalization``, ``reject_audit``, ``regression_runs``,
    ``blind_results`` (superseded blind batches only), ``expert_agreement``,
    ``lab_name_collisions`` and ``latest_cut`` (N, K, tie-class size; None before a cut)."""
    epoch = await _report_epoch(db)
    epoch_id = epoch.id if epoch is not None else None
    return {
        "epoch_id": str(epoch_id) if epoch_id else None,
        "counts": {
            "by_stage": await _stage_counts(db, epoch_id),
            "by_source": await _grouped(db, _document_source()),
            "by_institution": await _grouped(db, LandscapeDocument.qualifying_institution),
        },
        "refusals": {
            "by_stage": await _refusals_by_stage(db),
            "by_field": await _refusals_by_field(db, epoch_id),
        },
        "spend_by_day": await _spend(db),
        "cost_per_useful_opportunity": await _cost_per_useful(db),
        "completeness": await _completeness(db),
        "quote_verification": await _quote_rate(db, epoch_id),
        "normalization": await _normalization(db, epoch_id),
        "reject_audit": await _audit(db, epoch_id),
        "regression_runs": await _regression_runs(db),
        "blind_results": await _blind_results(db),
        "expert_agreement": await _expert_agreement(db),
        "lab_name_collisions": await _collisions(db),
        "latest_cut": await _latest_cut(db, epoch_id),
    }


def _institutions_of(authorships: list | None):
    for authorship in authorships or []:
        if isinstance(authorship, dict):
            for inst in authorship.get("institutions") or []:
                if isinstance(inst, dict):
                    yield inst


def _hopkins_like(inst: dict) -> bool:
    lineage = {_bare(i) for i in inst.get("lineage") or []}
    return bool(_HOPKINS_NAME.search(str(inst.get("display_name") or ""))) or bool(
        lineage & _LINEAGE_ROOTS
    )


async def drift_report(db: AsyncSession) -> dict:
    """The §5.1 drift listing: OpenAlex institutions on stored qualifying authorships that look
    Hopkins (the name says Hopkins or Kennedy Krieger, or the lineage reaches JHU or JH
    Medicine) and are in neither the Baltimore set nor its excluded list. It proposes; a code
    change adds (D1). Returns ``{"unknown_institutions": [{"id", "display_name", "documents"}],
    "documents_checked"}``, most frequent first."""
    seen: dict[str, dict] = {}
    checked = 0
    for authorships in (await db.execute(
        select(LandscapeDocument.qualifying_authorships).where(
            LandscapeDocument.merged_into.is_(None),
            LandscapeDocument.qualifying_authorships.is_not(None),
        )
    )).scalars():
        checked += 1
        for inst in _institutions_of(authorships):
            inst_id = _bare(inst.get("id"))
            if (not inst_id or inst_id in BALTIMORE_OPENALEX_IDS
                    or inst_id in EXCLUDED_OPENALEX_IDS or not _hopkins_like(inst)):
                continue
            entry = seen.setdefault(inst_id, {
                "id": inst_id, "display_name": inst.get("display_name"), "documents": 0,
            })
            entry["documents"] += 1
    listed = sorted(seen.values(), key=lambda e: (-e["documents"], e["id"]))
    return {"unknown_institutions": listed, "documents_checked": checked}


async def _setting(db: AsyncSession, key: str) -> str | None:
    return (await db.execute(
        select(AppSetting.value).where(AppSetting.key == key)
    )).scalar_one_or_none()


async def _latest(db: AsyncSession, model, *where, order) -> Any:
    return (await db.execute(select(model).where(*where).order_by(order.desc()).limit(1))
            ).scalars().first()


def _verdict(status: str | None) -> str:
    """``passed``, ``dropped`` or ``pending`` for one stage row's status."""
    if status is None:
        return "pending"
    if status in _PASSED:
        return "passed"
    if status.startswith(("excluded:", "failed:")) or status == "refused":
        return "dropped"
    return "pending"


async def _trace_ideas(db: AsyncSession, doc_id: uuid.UUID, epoch_id: uuid.UUID) -> list:
    """The epoch's ideas extracted from the document."""
    return list((await db.execute(
        select(LandscapeIdeaEvidence.idea_id).distinct()
        .join(LandscapeExtraction, LandscapeExtraction.id == LandscapeIdeaEvidence.extraction_id)
        .where(LandscapeExtraction.document_id == doc_id, LandscapeExtraction.epoch_id == epoch_id)
    )).scalars())


async def _s4_s5(db: AsyncSession, ideas: list, epoch_id: uuid.UUID) -> dict:
    """S4 (each idea's newest triage row) and S5 (any passed idea, or one folded into it, in
    the current cut's top K, which in the pilot is the cut over the pilot pool)."""
    T = LandscapeTriage
    passed = []
    verdicts = []
    for idea_id in ideas:
        row = await _latest(db, T, T.idea_id == idea_id, T.epoch_id == epoch_id,
                            order=T.created_at)
        verdict = _verdict(row.status if row is not None else None)
        verdicts.append(verdict)
        if verdict == "passed":
            passed.append(str(idea_id))
    s4 = "passed" if passed else ("pending" if "pending" in verdicts else "dropped")
    if s4 != "passed":
        return {"S4": s4}
    cut = await _current_cut(db, epoch_id)
    if cut is None:
        return {"S4": s4, "S5": "pending"}
    top = set((cut.ordered_idea_ids or [])[:cut.k])
    folded = (cut.folds or {}).get("folded") or {}
    in_cut = any(i in top for i in passed) or any(
        keeper in top and set(passed) & set(members) for keeper, members in folded.items()
    )
    return {"S4": s4, "S5": "passed" if in_cut else "dropped"}


async def _trace_stages(db: AsyncSession, doc: LandscapeDocument, epoch_id: uuid.UUID | None
                        ) -> dict[str, str]:
    """Each stage's verdict for one control document, stopping at the first that is not
    ``passed``."""
    stages = {"S1": "passed" if doc.s1_status == "eligible" else "dropped"}
    if stages["S1"] != "passed" or epoch_id is None:
        return stages
    S = LandscapeScreen
    screen = await _latest(db, S, S.document_id == doc.id, S.epoch_id == epoch_id,
                           order=S.created_at)
    stages["S2"] = _verdict(screen.status if screen is not None else None)
    if stages["S2"] != "passed":
        return stages
    E = LandscapeExtraction
    extractions = (await db.execute(
        select(E.status).where(E.document_id == doc.id, E.epoch_id == epoch_id)
    )).scalars().all()
    ideas = await _trace_ideas(db, doc.id, epoch_id)
    if ideas:
        stages["S3"] = "passed"
    elif any(_verdict(s) == "pending" for s in extractions) or not extractions:
        stages["S3"] = "pending"
    else:
        stages["S3"] = "dropped"
    if stages["S3"] != "passed":
        return stages
    stages.update(await _s4_s5(db, ideas, epoch_id))
    return stages


async def positive_control_trace(db: AsyncSession) -> dict:
    """The §10.2 positive-control trace: each ``app_settings.landscape_positive_controls``
    identifier through S1-S5 of the report epoch (module docstring), S5 judged against that
    epoch's current cut.

    Returns ``{"epoch_id", "controls": [{"identifier", "document_id", "stages": {"S1": …},
    "dropped_at", "pending_at"}], "note"}``. A stage's verdict is ``passed``, ``dropped`` or
    ``pending``, and the trace stops at the first that is not ``passed``; ``dropped_at`` names
    the stage that dropped the control. A control not stored yet has ``document_id`` None and
    is pending at S1. ``note`` says so when no control was entered (§10.2), else is None."""
    controls = intake._parse_controls(await _setting(db, "landscape_positive_controls"))
    epoch = await _report_epoch(db)
    epoch_id = epoch.id if epoch is not None else None
    traced = []
    for scheme, value in controls:
        doc_id = await intake.find_document(db, **{scheme: value})
        doc = await db.get(LandscapeDocument, doc_id) if doc_id is not None else None
        stages = await _trace_stages(db, doc, epoch_id) if doc is not None else {"S1": "pending"}
        traced.append({
            "identifier": f"{scheme}:{value}",
            "document_id": str(doc_id) if doc_id else None,
            "stages": stages,
            "dropped_at": next((s for s, v in stages.items() if v == "dropped"), None),
            "pending_at": next((s for s, v in stages.items() if v == "pending"), None),
        })
    return {
        "epoch_id": str(epoch_id) if epoch_id else None,
        "controls": traced,
        "note": None if controls else "No positive control was entered.",
    }


# --- the pilot report ------------------------------------------------------------------------


async def _probe_findings(db: AsyncSession) -> dict[str, dict]:
    """The newest ``probes`` report per probe (payload ``{"probe", "at", "finding"}``)."""
    out: dict[str, dict] = {}
    for row in (await db.execute(
        select(LandscapeReport).where(LandscapeReport.kind == "probes")
        .order_by(LandscapeReport.created_at.desc())
    )).scalars():
        payload = row.payload or {}
        out.setdefault(str(payload.get("probe") or row.id), payload)
    return out


async def _bakeoff(db: AsyncSession) -> dict | None:
    R = LandscapeReport
    row = await _latest(db, R, R.kind == "bakeoff", order=R.created_at)
    return None if row is None else {"report_id": str(row.id), "created_at": _iso(row.created_at),
                                     "payload": row.payload}


async def _pilot_completeness(db: AsyncSession) -> dict:
    """Completeness of every pilot slice (its newest fetch) and, per source, received ÷
    expected over those slices (§10.2: ≥ 0.99 each)."""
    anchor, seed = await intake._pilot_anchor(db, datetime.now(UTC))
    slices = intake.pilot_slices(today=anchor, seed=seed)
    F = LandscapeFetch
    rows, totals = [], defaultdict(lambda: [0, 0])
    for source, key in slices:
        fetch = await _latest(db, F, F.source == source, F.slice_key == key, order=F.started_at)
        if fetch is None:
            rows.append({"source": source, "slice_key": key, "status": "not_fetched"})
            continue
        expected, received = fetch.expected_count or 0, fetch.received_count or 0
        totals[source][0] += expected
        totals[source][1] += received
        rows.append({"source": source, "slice_key": key, "status": fetch.status,
                     "expected": fetch.expected_count, "received": fetch.received_count,
                     "ratio": _rate(received, expected)})
    by_source = {s: _rate(r, e) for s, (e, r) in totals.items()}
    return {"slices": rows, "by_source": by_source}


async def _truncation(db: AsyncSession) -> dict[str, dict]:
    """Per S6/S9 ledger stage: calls, and calls whose output reached the stage's first-attempt
    ``max_tokens`` constant, the ledger's sign of a ``max_tokens`` stop (m-O)."""
    C = LandscapeLlmCall
    out = {}
    for stage in _TRUNCATION_STAGES:
        limit = STAGE_SPECS[stage].max_tokens
        calls, truncated = (await db.execute(
            select(func.count(), func.count().filter(C.output_tokens >= limit))
            .where(C.stage == stage)
        )).one()
        out[stage] = {"calls": calls, "truncated": truncated, "rate": _rate(truncated, calls),
                      "max_tokens": limit}
    return out


async def _cost_throughput(db: AsyncSession) -> dict:
    """Per ledger stage: calls, dollars and calls per hour over the stage's span, each
    projected to the full window by the ratio of window months to pilot months (one per
    window year, D68)."""
    anchor, _ = await intake._pilot_anchor(db, datetime.now(UTC))
    months = intake._window_months(anchor, get_settings().landscape_window_days)
    factor = len(months) / max(1, len({y for y, _ in months}))
    C = LandscapeLlmCall
    rows = (await db.execute(
        select(C.stage, func.count(), func.coalesce(func.sum(C.cost_usd), 0),
               func.min(C.created_at), func.max(C.created_at)).group_by(C.stage)
    )).all()
    stages = {}
    for stage, calls, cost, first, last in rows:
        hours = (last - first).total_seconds() / 3600 if first and last else 0
        stages[stage] = {
            "calls": calls, "cost_usd": float(cost),
            "calls_per_hour": calls / hours if hours > 0 else None,
            "projected_calls": calls * factor, "projected_cost_usd": float(cost) * factor,
            "projected_hours": hours * factor if hours > 0 else None,
        }
    return {"backfill_factor": factor, "stages": stages}


async def pilot_report(db_factory, *, s4_repeatability: dict | None = None) -> uuid.UUID:
    """Store and return the id of a ``LandscapeReport("pilot")`` holding (D68, spec §15):

    ``bakeoff`` (the newest bake-off report, or None), ``probes`` (the newest finding per
    probe), ``completeness`` (per pilot slice and source), ``s4_repeatability`` (the
    `s4_repeatability` result the worker passes; without one,
    ``eval.bakeoff.run_s4_repeatability`` runs here, so it makes model calls),
    ``normalization`` (rate and ambiguous-name count), ``positive_controls``
    (`positive_control_trace`, with its note when none was entered), ``truncation`` (S6 and
    S9 rates from the ledger) and ``cost_throughput`` (per stage, projected for the
    backfill)."""
    repeatability = (
        s4_repeatability if s4_repeatability is not None
        else await bakeoff.run_s4_repeatability(db_factory)
    )
    async with db_factory() as db:
        epoch = await _report_epoch(db)
        epoch_id = epoch.id if epoch is not None else None
        payload = {
            "generated_at": datetime.now(UTC).isoformat(),
            "epoch_id": str(epoch_id) if epoch_id else None,
            "bakeoff": await _bakeoff(db),
            "probes": await _probe_findings(db),
            "completeness": await _pilot_completeness(db),
            "s4_repeatability": repeatability,
            "normalization": await _normalization(db, epoch_id),
            "positive_controls": await positive_control_trace(db),
            "truncation": await _truncation(db),
            "cost_throughput": await _cost_throughput(db),
        }
        report_id = uuid.uuid4()
        db.add(LandscapeReport(
            id=report_id, kind="pilot", payload=json.loads(json.dumps(payload, default=str)),
            created_at=func.clock_timestamp(),
        ))
        await db.commit()
        return report_id
