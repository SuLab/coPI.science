"""The regression set and its gate on candidate epochs (spec §10.4, §7.11 step 1; L28, X9, D54,
D65).

**The set** is versioned: every addition is a new ``LandscapeRegressionSet`` version, so a
version never changes once frozen. Items (``LandscapeRegressionItem``) carry the request input
the stage reads (``item_ref``, a user message or the two texts of a pair) and the output to
compare with (``expected``). Item kinds and the stages whose change runs them:

- ``screen``, ``extract``, ``triage`` (S2-S4): the bake-off's items with the Opus 5 references
  (origin ``bakeoff``, carried into every later version); positive controls (S2) and rescues
  (S2, S4, S6), which expect only that the item is kept (``{"kept": True}``);
- ``deep`` (S6) and ``final`` (S9): 20 of the active epoch's current opportunities, with their
  stored S6 and S9 outputs (origin ``frozen``);
- ``pair`` (S7 and the S5 tie-break): 30 answered comparisons of the active epoch;
- ``similarity`` (S8): 30 similarity verdicts of the active epoch; labels are never scored;
- ``expert``: submitted expert judgments (§10.6), with the active epoch's recommendation.

**The gate** (``run_regression``) asks the candidate epoch's own prompts and models
(``epochs.epoch_inputs``) for each item of the stages its change touches, and holds the epoch
when any stage misses its threshold. With no active epoch only the S2-S4 items are checked
(D65). The candidate is judged on the set version current when it was created, or the first
version when it predates every version.

Reading the S6 dossier, the S9 message and the S7/S8 texts reuses the stage modules' own
builders, so a regression request is the production request (D32). The S6 item is asked as
S6's final round (no tools): its search loop is not replayed.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.landscaping import comparator, epochs, intake
from src.landscaping.epochs import EpochInputs
from src.landscaping.eval.bakeoff import (
    KEPT,
    TRIAGE_MIN_GATES,
    TRIAGE_MIN_SCORES,
    Answer,
    ask_all,
    extract_metrics,
    gate_score_metrics,
    new_set_version,
    prepare_extraction,
    screen_metrics,
    stage_request,
    total_cost,
)
from src.landscaping.stages import deep, final, opportunities, screen
from src.models.landscape import (
    LandscapeAssessment,
    LandscapeComparison,
    LandscapeDeep,
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeExpertJudgment,
    LandscapeExtraction,
    LandscapeIdea,
    LandscapeOpportunity,
    LandscapeOpportunityVersion,
    LandscapeRegressionItem,
    LandscapeRegressionRun,
    LandscapeRegressionSet,
    LandscapeRescue,
    LandscapeScreen,
    LandscapeSimilarity,
    LandscapeText,
    LandscapeTriage,
)

#: Item kind -> the stages whose change runs it (§10.4 "for the stages the change touches").
#: The rerank model and prompt judge both S7 and the S5 tie-break.
KIND_STAGES: dict[str, tuple[str, ...]] = {
    "screen": ("screen",),
    "extract": ("extract",),
    "triage": ("triage",),
    "deep": ("deep",),
    "pair": ("cut", "rerank"),
    "similarity": ("similarity",),
    "final": ("final",),
    "expert": ("final",),
}
#: All an epoch is checked on while none is active (D65).
FIRST_EPOCH_KINDS = ("screen", "extract", "triage")

# The §10.4 pass rule for S6-S9 (S2-S4 use the §10.1 thresholds in ``eval.bakeoff``).
DEEP_MIN_GATES = 0.90
DEEP_MIN_SCORES = 0.90
PAIR_MIN_AGREEMENT = 0.80
SIMILARITY_MIN_AGREEMENT = 0.85
FINAL_MIN_RECOMMENDATION = 0.80
FINAL_MIN_GATES = 0.90
FINAL_MIN_SCORES = 0.90

# How much a frozen version takes from the active epoch (§10.4).
FROZEN_OPPORTUNITIES = 20
FROZEN_PAIRS = 30
FROZEN_SIMILARITY = 30

_KEPT_EXPECTED = {"kept": True}
# A rescue's ``item_kind`` or ``stage`` -> the regression kind of its item.
_RESCUE_KINDS = {
    "screen": "screen", "s2": "screen", "landscape_screens": "screen",
    "triage": "triage", "s4": "triage", "landscape_triage": "triage",
    "deep": "deep", "s6": "deep", "deep_gate": "deep", "landscape_deep": "deep",
}


def _item(kind: str, origin: str, ref: dict, expected: dict) -> dict:
    return {"item_kind": kind, "origin": origin, "item_ref": ref, "expected": expected}


# --- freezing a version -------------------------------------------------------------------------


async def _deep_dossier(db_factory, idea_id: uuid.UUID, epoch_id: uuid.UUID) -> str:
    """The idea's S6 user message as S6 builds it (``deep._dossier``): the full texts already
    stored, the epoch's stored full-text extractions, and the database records through the
    evidence cache (``deep._records``, which looks up a record the cache lacks). No full text is
    fetched and nothing is re-extracted."""
    async with db_factory() as db:
        facts = await deep._facts(db, idea_id, epoch_id)
        doc_ids = [doc.id for doc in facts.documents]
        texts, extracted = {}, {}
        if doc_ids:
            texts = {
                row.document_id: deep._stored_text(row)
                for row in (
                    await db.scalars(
                        select(LandscapeText).where(
                            LandscapeText.document_id.in_(doc_ids),
                            LandscapeText.text_basis == "full_text",
                        )
                    )
                ).all()
            }
            extracted = {
                row.document_id: row.raw or {}
                for row in (
                    await db.scalars(
                        select(LandscapeExtraction).where(
                            LandscapeExtraction.epoch_id == epoch_id,
                            LandscapeExtraction.basis == "full_text",
                            LandscapeExtraction.status == "done",
                            LandscapeExtraction.document_id.in_(doc_ids),
                        )
                    )
                ).all()
            }
        await db.commit()
    records = await deep._records(db_factory, facts.idea)
    return deep._dossier(facts, texts, extracted, records)


def _verdict_expected(row: LandscapeAssessment) -> dict:
    return {"recommendation": row.recommendation, "gating": row.gating or {},
            "scores": row.scores or {}}


async def _opportunity_items(db_factory, epoch_id: uuid.UUID) -> list[dict]:
    """S9 and S6 items: up to ``FROZEN_OPPORTUNITIES`` of the epoch's current, unretired,
    non-probe opportunities assessed ``done`` in it, and each member's ``done`` S6 record."""
    A, V, Opp = LandscapeAssessment, LandscapeOpportunityVersion, LandscapeOpportunity
    items: list[dict] = []
    members: dict[uuid.UUID, LandscapeDeep] = {}
    async with db_factory() as db:
        rows = (
            await db.execute(
                select(A, V.member_idea_keys)
                .join(V, and_(V.opportunity_id == A.opportunity_id,
                              V.version == A.opportunity_version))
                .join(Opp, Opp.id == A.opportunity_id)
                .where(
                    A.epoch_id == epoch_id, A.status == "done", V.is_probe.is_(False),
                    Opp.retired_at.is_(None), V.version == Opp.version_counter,
                )
                .order_by(func.random())
                .limit(FROZEN_OPPORTUNITIES)
            )
        ).all()
        for assessment, keys in rows:
            message = await final._message_for(db, assessment)
            if message is None:
                continue
            items.append(_item(
                "final", "frozen",
                {"opportunity_id": str(assessment.opportunity_id),
                 "opportunity_version": assessment.opportunity_version, "user": message},
                _verdict_expected(assessment),
            ))
            for idea, record in await final._members(db, epoch_id, keys) or []:
                members.setdefault(idea.id, record)
        await db.commit()
    for idea_id, record in members.items():
        items.append(_item(
            "deep", "frozen",
            {"idea_id": str(idea_id), "idea_version": record.idea_version,
             "user": await _deep_dossier(db_factory, idea_id, epoch_id)},
            {"gating": record.gating or {}, "scores": record.provisional_scores or {}},
        ))
    return items


async def _pair_items(db: AsyncSession, epoch_id: uuid.UUID) -> list[dict]:
    """Up to ``FROZEN_PAIRS`` answered comparisons (S7 and the S5 tie-break), each with the two
    texts in the order they were shown and the winner as ``A`` or ``B``."""
    C = LandscapeComparison
    rows = (
        await db.scalars(
            select(C)
            .where(C.epoch_id == epoch_id, C.status == "done", C.winner_idea_id.is_not(None))
            .order_by(func.random())
            .limit(FROZEN_PAIRS)
        )
    ).all()
    if not rows:
        return []
    texts = await comparator._texts(db, rows)
    items = []
    for row in rows:
        first, second = comparator._shown_texts(row, texts)
        winner = "A" if row.winner_idea_id == comparator._shown(row)[0] else "B"
        items.append(_item(
            "pair", "frozen",
            {"comparison_id": str(row.id), "basis": row.basis, "first": first, "second": second},
            {"winner": winner},
        ))
    return items


async def _similarity_items(db: AsyncSession, epoch_id: uuid.UUID) -> list[dict]:
    """Up to ``FROZEN_SIMILARITY`` same/different verdicts, each with the user message S8's
    similarity call reads (``opportunities._idea_texts``)."""
    S = LandscapeSimilarity
    rows = (
        await db.scalars(
            select(S)
            .where(S.epoch_id == epoch_id, S.verdict.in_(
                (opportunities.SAME_PROGRAM, opportunities.DIFFERENT_PROGRAM)
            ))
            .order_by(func.random())
            .limit(FROZEN_SIMILARITY)
        )
    ).all()
    items = []
    for row in rows:
        found = {
            idea.id: idea
            for idea in (
                await db.scalars(
                    select(LandscapeIdea).where(LandscapeIdea.id.in_((row.idea_a, row.idea_b)))
                )
            ).all()
        }
        if len(found) != 2:
            continue
        texts = await opportunities._idea_texts(
            db, epoch_id, [found[row.idea_a], found[row.idea_b]]
        )
        items.append(_item(
            "similarity", "frozen",
            {"similarity_id": str(row.id),
             "user": f"# Idea A\n\n{texts[row.idea_a]}\n\n# Idea B\n\n{texts[row.idea_b]}"},
            {"verdict": row.verdict},
        ))
    return items


async def _positive_control_items(db: AsyncSession) -> list[dict]:
    """One S2 item per stored positive control (``app_settings.landscape_positive_controls``),
    which must be kept."""
    raw = await intake._setting(db, "landscape_positive_controls")
    items, seen = [], set()
    for scheme, value in intake._parse_controls(raw):
        doc_id = await intake.find_document(db, **{scheme: value})
        if doc_id is None or doc_id in seen:
            continue
        seen.add(doc_id)
        doc = await db.get(LandscapeDocument, doc_id)
        items.append(_item(
            "screen", "positive_control",
            {"document_id": str(doc_id), "control": f"{scheme}:{value}",
             "user": screen.user_message(doc)},
            _KEPT_EXPECTED,
        ))
    return items


def _rescue_kind(rescue: LandscapeRescue) -> str | None:
    for value in (rescue.item_kind, rescue.stage):
        kind = _RESCUE_KINDS.get((value or "").strip().lower())
        if kind is not None:
            return kind
    return None


async def _rescue_ref(db: AsyncSession, kind: str, item_id: uuid.UUID) -> dict | None:
    """The S2 or S4 input of a rescued stage row, or None when it is gone."""
    if kind == "screen":
        row = await db.get(LandscapeScreen, item_id)
        doc = await db.get(LandscapeDocument, row.document_id) if row is not None else None
        if doc is None:
            return None
        return {"document_id": str(doc.id), "user": screen.user_message(doc)}
    row = await db.get(LandscapeTriage, item_id)
    dossier = (row.raw or {}).get("dossier") if row is not None else None
    return {"user": dossier} if dossier else None


async def _rescue_items(db_factory) -> list[dict]:
    """One item per rescued stage row (§10.5), at its stage, which must be kept."""
    items: list[dict] = []
    deep_rows: list[tuple[dict, uuid.UUID, uuid.UUID]] = []
    seen: set[tuple[str, uuid.UUID]] = set()
    async with db_factory() as db:
        rescues = (
            await db.scalars(select(LandscapeRescue).order_by(LandscapeRescue.created_at))
        ).all()
        for rescue in rescues:
            kind = _rescue_kind(rescue)
            if kind is None or (kind, rescue.item_id) in seen:
                continue
            seen.add((kind, rescue.item_id))
            base = {"rescue_id": str(rescue.id), "item_id": str(rescue.item_id)}
            if kind == "deep":
                row = await db.get(LandscapeDeep, rescue.item_id)
                if row is not None:
                    deep_rows.append((base, row.idea_id, row.epoch_id))
                continue
            ref = await _rescue_ref(db, kind, rescue.item_id)
            if ref is not None:
                items.append(_item(kind, "rescue", {**base, **ref}, _KEPT_EXPECTED))
        await db.commit()
    for base, idea_id, epoch_id in deep_rows:
        user = await _deep_dossier(db_factory, idea_id, epoch_id)
        items.append(_item("deep", "rescue", {**base, "user": user}, _KEPT_EXPECTED))
    return items


async def _compared_assessment(
    db: AsyncSession, judgment: LandscapeExpertJudgment, active_id: uuid.UUID | None
) -> LandscapeAssessment | None:
    """The active epoch's newest ``done`` assessment of the judged opportunity, else the
    assessment the judgment was compared with."""
    A = LandscapeAssessment
    if active_id is not None:
        found = await db.scalar(
            select(A)
            .where(A.epoch_id == active_id, A.opportunity_id == judgment.opportunity_id,
                   A.status == "done")
            .order_by(A.opportunity_version.desc())
            .limit(1)
        )
        if found is not None:
            return found
    if judgment.landscape_assessment_id is None:
        return None
    return await db.get(A, judgment.landscape_assessment_id)


async def _expert_items(db: AsyncSession, active_id: uuid.UUID | None) -> list[dict]:
    """One item per submitted expert judgment with a recommendation (§10.6): the S9 message of
    the compared assessment, the expert's recommendation and the active epoch's."""
    J = LandscapeExpertJudgment
    judgments = (
        await db.scalars(
            select(J)
            .where(J.submitted_at.is_not(None), J.recommendation.is_not(None))
            .order_by(J.submitted_at, J.id)
        )
    ).all()
    items = []
    for judgment in judgments:
        assessment = await _compared_assessment(db, judgment, active_id)
        message = await final._message_for(db, assessment) if assessment is not None else None
        if message is None:
            continue
        items.append(_item(
            "expert", "expert",
            {"judgment_id": str(judgment.id), "opportunity_id": str(judgment.opportunity_id),
             "user": message},
            {"recommendation": judgment.recommendation,
             "active_recommendation": assessment.recommendation},
        ))
    return items


async def _carried_items(db: AsyncSession, version: int | None) -> list[dict]:
    """The bake-off's S2-S4 items of ``version``, which every later version keeps."""
    if version is None:
        return []
    Item = LandscapeRegressionItem
    rows = (
        await db.scalars(
            select(Item)
            .where(Item.set_version == version, Item.origin == "bakeoff")
            .order_by(Item.created_at, Item.id)
        )
    ).all()
    return [_item(r.item_kind, r.origin, r.item_ref, r.expected) for r in rows]


async def freeze_regression_set(db_factory, *, reason: str) -> int:
    """Freeze a new regression-set version (spec §10.4) and return its number.

    It holds the previous version's bake-off items; from the active epoch, if any, its S6/S9
    items, comparison pairs and similarity pairs with their stored outputs; the positive
    controls; the rescued items; and the submitted expert judgments. ``reason`` is recorded as
    the version's notes. The version and its items are written in one transaction; the only
    other writes are evidence-cache rows the S6 dossiers' record lookups add.
    """
    async with db_factory() as db:
        active = await epochs.active_epoch(db)
        active_id = active.id if active is not None else None
        previous = await db.scalar(select(func.max(LandscapeRegressionSet.version)))
        items = await _carried_items(db, previous)
        items += await _positive_control_items(db)
        if active_id is not None:
            items += await _pair_items(db, active_id)
            items += await _similarity_items(db, active_id)
        items += await _expert_items(db, active_id)
        await db.commit()
    if active_id is not None:
        items += await _opportunity_items(db_factory, active_id)
    items += await _rescue_items(db_factory)
    async with db_factory() as db:
        version = await new_set_version(db, notes=reason)
        db.add_all([LandscapeRegressionItem(set_version=version, **item) for item in items])
        await db.commit()
    return version


async def refresh_after_activation(db_factory, *, epoch_id: uuid.UUID) -> int:
    """The baseline refresh after an activation (D65): ``freeze_regression_set`` on the newly
    active ``epoch_id``. ``ValueError`` when ``epoch_id`` is not the active epoch."""
    async with db_factory() as db:
        active = await epochs.active_epoch(db)
    if active is None or active.id != epoch_id:
        raise ValueError(f"epoch {epoch_id} is not the active epoch")
    return await freeze_regression_set(
        db_factory, reason=f"baseline after activating epoch {epoch_id} (D65)"
    )


# --- the gate ---------------------------------------------------------------------------------


def kinds_to_check(changed: list[str] | None, *, first_epoch: bool) -> list[str]:
    """The item kinds a candidate is checked on: those reading a stage in ``changed`` (every
    stage when None), and only S2-S4 for a first epoch (D65)."""
    stages = set(changed) if changed is not None else {s for v in KIND_STAGES.values() for s in v}
    allowed = FIRST_EPOCH_KINDS if first_epoch else tuple(KIND_STAGES)
    return [kind for kind in allowed if stages & set(KIND_STAGES[kind])]


async def _set_version_for(db: AsyncSession, epoch: LandscapeEpoch) -> int:
    """The version the candidate is judged on: the one already recorded on it, else the newest
    created by the time it was, else the first (the bake-off's). ``LookupError`` when no
    version exists: the first candidate's regression waits for the bake-off (§7.11 step 1)."""
    if epoch.regression_set_version is not None:
        return epoch.regression_set_version
    S = LandscapeRegressionSet
    version = await db.scalar(select(func.max(S.version)).where(S.created_at <= epoch.created_at))
    if version is None:
        version = await db.scalar(select(func.min(S.version)))
    if version is None:
        raise LookupError("no regression-set version exists yet; the bake-off seeds the first")
    return version


async def _ask_stage(
    stage: str, requests: dict[str, dict], *, inputs: EpochInputs, db_factory
) -> dict[str, Answer]:
    return await ask_all(
        stage, requests, db_factory=db_factory, ref_prefix=f"regression:{inputs.epoch_id}"
    )


async def _ask_users(
    stage: str, items: list[LandscapeRegressionItem], *, inputs: EpochInputs, db_factory
) -> dict[str, Answer]:
    """Each item's stored user message sent with the candidate's own ``stage`` prompt and
    model; keyed by item id."""
    model, system = inputs.model(stage), inputs.system(stage)
    requests = {
        str(item.id): stage_request(stage, item.item_ref["user"], model=model, system=system)
        for item in items
    }
    return await _ask_stage(stage, requests, inputs=inputs, db_factory=db_factory)


def _agreement(pairs: list[tuple[Any, Any]], minimum: float) -> dict:
    hits = sum(1 for expected, got in pairs if got is not None and got == expected)
    share = hits / len(pairs) if pairs else 1.0
    return {"items": len(pairs), "agreement": share, "checks": {"agreement": share >= minimum},
            "passed": share >= minimum}


def _fold(value: Any) -> str | None:
    return " ".join(value.casefold().split()) if isinstance(value, str) else None


async def _check_screen(items, *, inputs, db_factory, docs) -> dict:
    answers = await _ask_users("screen", items, inputs=inputs, db_factory=db_factory)
    scored = [
        (bool(item.expected.get("kept")) or item.expected.get("decision") in KEPT,
         answers[str(item.id)])
        for item in items
    ]
    return {**screen_metrics(scored, list(answers.values())),
            "cost_usd": str(total_cost(answers.values()))}


async def _check_extract(items, *, inputs, db_factory, docs) -> dict:
    items = [item for item in items if item.item_ref.get("document_id") in docs]
    answers = await _ask_users("extract", items, inputs=inputs, db_factory=db_factory)
    pairs = []
    for item in items:
        doc = docs[item.item_ref["document_id"]]
        got = await prepare_extraction(answers[str(item.id)].parsed, doc, inputs.criteria)
        pairs.append((frozenset(tuple(k) for k in item.expected.get("keys") or []), got))
    return {**extract_metrics(pairs), "cost_usd": str(total_cost(answers.values()))}


async def _check_triage(items, *, inputs, db_factory, docs) -> dict:
    answers = await _ask_users("triage", items, inputs=inputs, db_factory=db_factory)
    pairs = [(item.expected, answers[str(item.id)].parsed) for item in items]
    return {**gate_score_metrics(pairs, gates_min=TRIAGE_MIN_GATES, scores_min=TRIAGE_MIN_SCORES),
            "cost_usd": str(total_cost(answers.values()))}


async def _check_deep(items, *, inputs, db_factory, docs) -> dict:
    model, system = inputs.model("deep"), inputs.system("deep")
    requests = {}
    for item in items:
        request = stage_request("deep", item.item_ref["user"], model=model, system=system)
        # S6's final round: no tools, the closing instruction appended (deep._analysis).
        request["messages"] = deep._with_final_text(request["messages"])
        requests[str(item.id)] = request
    answers = await _ask_stage("deep", requests, inputs=inputs, db_factory=db_factory)
    pairs = [(item.expected, answers[str(item.id)].parsed) for item in items]
    return {**gate_score_metrics(pairs, gates_min=DEEP_MIN_GATES, scores_min=DEEP_MIN_SCORES),
            "cost_usd": str(total_cost(answers.values()))}


async def _check_pair(items, *, inputs, db_factory, docs) -> dict:
    system, model = inputs.system("rerank"), inputs.model("rerank")
    requests = {
        str(item.id): comparator._request(
            item.item_ref["first"], item.item_ref["second"], system=system, model=model,
            retry=False,
        )
        for item in items
    }
    answers = await _ask_stage("rerank", requests, inputs=inputs, db_factory=db_factory)
    pairs = [
        (item.expected.get("winner"), (answers[str(item.id)].parsed or {}).get("winner"))
        for item in items
    ]
    return {**_agreement(pairs, PAIR_MIN_AGREEMENT),
            "cost_usd": str(total_cost(answers.values()))}


def _verdict_of(parsed: dict | None) -> str | None:
    same = (parsed or {}).get("same_program")
    if not isinstance(same, bool):
        return None
    return opportunities.SAME_PROGRAM if same else opportunities.DIFFERENT_PROGRAM


async def _check_similarity(items, *, inputs, db_factory, docs) -> dict:
    answers = await _ask_users("similarity", items, inputs=inputs, db_factory=db_factory)
    pairs = [
        (item.expected.get("verdict"), _verdict_of(answers[str(item.id)].parsed))
        for item in items
    ]
    return {**_agreement(pairs, SIMILARITY_MIN_AGREEMENT),
            "cost_usd": str(total_cost(answers.values()))}


def _final_view(parsed: dict | None, inputs: EpochInputs) -> dict | None:
    """The validated S9 verdict's gates, scores and recommendation (``final._verdict_values``
    with the candidate's rubric), or None for an invalid reply."""
    try:
        values = final._verdict_values(parsed, inputs.rubric)
    except ValueError:
        return None
    return {"gating": values["gating"], "scores": values["scores"],
            "recommendation": values.get("recommendation")}


async def _check_final(items, *, inputs, db_factory, docs) -> dict:
    answers = await _ask_users("final", items, inputs=inputs, db_factory=db_factory)
    views = {str(item.id): _final_view(answers[str(item.id)].parsed, inputs) for item in items}
    recommendation = _agreement(
        [(_fold(item.expected.get("recommendation")),
          _fold((views[str(item.id)] or {}).get("recommendation")))
         for item in items],
        FINAL_MIN_RECOMMENDATION,
    )
    calls = gate_score_metrics(
        [(item.expected, views[str(item.id)]) for item in items],
        gates_min=FINAL_MIN_GATES, scores_min=FINAL_MIN_SCORES,
    )
    checks = {"recommendation": recommendation["passed"], **calls["checks"]}
    return {
        **calls,
        "recommendation_agreement": recommendation["agreement"],
        "checks": checks,
        "passed": all(checks.values()),
        "cost_usd": str(total_cost(answers.values())),
    }


async def _check_expert(items, *, inputs, db_factory, docs) -> dict:
    answers = await _ask_users("final", items, inputs=inputs, db_factory=db_factory)
    candidate = active = 0
    for item in items:
        expert = _fold(item.expected.get("recommendation"))
        view = _final_view(answers[str(item.id)].parsed, inputs)
        candidate += view is not None and _fold(view.get("recommendation")) == expert
        active += _fold(item.expected.get("active_recommendation")) == expert
    passed = candidate >= active
    return {"items": len(items), "candidate_agrees": candidate, "active_agrees": active,
            "checks": {"agreement": passed}, "passed": passed,
            "cost_usd": str(total_cost(answers.values()))}


_CHECKS: dict[str, Callable[..., Awaitable[dict]]] = {
    "screen": _check_screen,
    "extract": _check_extract,
    "triage": _check_triage,
    "deep": _check_deep,
    "pair": _check_pair,
    "similarity": _check_similarity,
    "final": _check_final,
    "expert": _check_expert,
}


async def _documents(
    db: AsyncSession, items: list[LandscapeRegressionItem]
) -> dict[str, LandscapeDocument]:
    """The stored documents the S3 items name, by id string; a missing one is left out."""
    ids = {
        uuid.UUID(item.item_ref["document_id"])
        for item in items
        if item.item_kind == "extract" and item.item_ref.get("document_id")
    }
    if not ids:
        return {}
    rows = (await db.scalars(select(LandscapeDocument).where(LandscapeDocument.id.in_(ids)))).all()
    return {str(doc.id): doc for doc in rows}


async def run_regression(db_factory, epoch_id: uuid.UUID) -> bool:
    """Run the regression gate on candidate epoch ``epoch_id`` (spec §10.4, §7.11 step 1);
    True when it passes.

    The epoch is judged on its set version (``_set_version_for``), for the item kinds of the
    stages its change touches (``kinds_to_check``; S2-S4 only while no epoch is active), each
    stage against its threshold: S2-S4 the §10.1 ones; S6 gates and scores ±1 ≥ 90%; S7 and
    the S5 tie-break the winner ≥ 80%; S8 the verdict ≥ 85%; S9 the recommendation ≥ 80%,
    gates and scores ±1 ≥ 90%; expert items at least as often as the active epoch.

    Records a ``LandscapeRegressionRun`` with the per-stage results and stamps the epoch's
    ``regression_set_version`` and ``regression_run_id``. On a failure the epoch is held
    (``epochs.hold``) with the failing stages as the reason; a pass leaves its status alone.
    ``LookupError`` for an unknown epoch or when no set version exists yet.
    """
    started = datetime.now(UTC)
    Item = LandscapeRegressionItem
    async with db_factory() as db:
        epoch = await db.get(LandscapeEpoch, epoch_id)
        if epoch is None:
            raise LookupError(f"epoch {epoch_id} does not exist")
        version = await _set_version_for(db, epoch)
        active = await epochs.active_epoch(db)
        first_epoch = active is None or active.id == epoch_id
        kinds = kinds_to_check(epoch.changed_stages, first_epoch=first_epoch)
        items = list(
            (
                await db.scalars(
                    select(Item)
                    .where(Item.set_version == version, Item.item_kind.in_(kinds))
                    .order_by(Item.created_at, Item.id)
                )
            ).all()
        )
        inputs = await epochs.epoch_inputs(db, epoch_id)
        docs = await _documents(db, items)
        await db.commit()
    by_kind: dict[str, list[LandscapeRegressionItem]] = defaultdict(list)
    for item in items:
        by_kind[item.item_kind].append(item)
    results = {
        kind: await _CHECKS[kind](by_kind[kind], inputs=inputs, db_factory=db_factory, docs=docs)
        for kind in kinds if by_kind[kind]
    }
    passed = all(result["passed"] for result in results.values())
    run_id = uuid.uuid4()
    async with db_factory() as db:
        db.add(LandscapeRegressionRun(
            id=run_id, epoch_id=epoch_id, set_version=version, started_at=started,
            finished_at=datetime.now(UTC), status="passed" if passed else "failed",
            results={"first_epoch": first_epoch, "kinds": kinds, "stages": results},
        ))
        await db.execute(
            update(LandscapeEpoch)
            .where(LandscapeEpoch.id == epoch_id)
            .values(regression_set_version=version, regression_run_id=run_id,
                    updated_at=func.now())
        )
        await db.commit()
        if not passed:
            failing = ", ".join(kind for kind, result in results.items() if not result["passed"])
            await epochs.hold(
                db, epoch_id, f"regression failed on set version {version}: {failing}"
            )
    return passed
