"""The Phase 0 bake-off (spec §10.1, D43, D68) and the S4 repeatability measurement (§10.2).

The bake-off picks the S2, S3 and S4 models. On a stratified sample of the pilot documents (by
OpenAlex topic, kind and year; D68), Claude Opus 5 is the reference and each candidate model
answers the same requests, built by the production builders (D32) with the judging epoch's own
prompts (``epochs.epoch_inputs``, §7.11):

- **S2:** ``sample_sizes[0]`` documents (``screen.user_message``).
- **S3:** ``sample_sizes[1]`` of the documents the reference kept (``yes`` or ``unsure``).
  Answers are quote-checked and normalized as S3 does (``extract._prepare``) before they are
  compared.
- **S4:** ``sample_sizes[2]`` ideas. The reference's S3 answers on every kept document are
  grouped in memory (``ideas.group_candidates``), each group's evidence comes from
  ``ideas.evidence_payload`` and its dossier from ``triage.triage_dossier``.

Everything is held in memory: no stage row of any epoch is written. What is written is the
ledger row of each call, evidence-cache rows, a ``LandscapeReport("bakeoff")`` and the first
items of the regression set (§10.4). The report carries the recommended stage settings. It never
changes ``.env``; an operator applies them.

The metric helpers here also serve the regression gate (``eval.regression``), which applies the
same §10.1 thresholds to a candidate epoch.
"""
from __future__ import annotations

import asyncio
import random
import uuid
from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.landscaping import epochs, ideas
from src.landscaping.criteria import Criteria
from src.landscaping.epochs import EpochInputs
from src.landscaping.llm_client import STAGE_SPECS, build_request, call_sync
from src.landscaping.schemas import SCHEMAS
from src.landscaping.scoring import TRIAGE_DIMENSIONS, excluded_reason
from src.landscaping.stages import extract, screen
from src.landscaping.stages.triage import triage_dossier
from src.models.landscape import (
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeRegressionItem,
    LandscapeRegressionSet,
    LandscapeReport,
    LandscapeTriage,
)
from src.services.llm_pricing import cost_for_request

#: The reference model of every bake-off stage (spec §10.1).
REFERENCE_MODEL = "claude-opus-5"
#: The candidates for S2-S4, in the order a cost tie is broken (spec §9.1).
CANDIDATE_MODELS = ("claude-haiku-5-5", "claude-sonnet-5", "claude-sonnet-5-5")

# The §10.1 pass thresholds; the regression gate applies them too (§10.4).
SCREEN_MAX_REFUSAL = 0.02
SCREEN_MIN_RECALL = 0.95
SCREEN_MIN_PRECISION = 0.6
EXTRACT_MIN_AGREEMENT = 0.90
EXTRACT_MIN_QUOTES = 0.95
TRIAGE_MIN_GATES = 0.90
TRIAGE_MIN_SCORES = 0.90

#: S2 answers that send a document on to S3.
KEPT = ("yes", "unsure")
# Calls in flight at once; each writes its own ledger row through its own session.
_CONCURRENCY = 8


# --- one model's answers ------------------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    """One model's answer to one request.

    ``refused_before``: the asked model refused, whether or not the fallback chain then served
    the item; ``refused_after``: the chain was exhausted (D42). ``parsed`` is None for a
    refusal, an error, a truncation or a reply with no JSON object. ``cost`` is the served
    attempt's cost at the batch rate, 0 when unpriced.
    """

    parsed: dict | None
    refused_before: bool
    refused_after: bool
    served_by: str
    cost: Decimal


def stage_request(stage: str, user: str, *, model: str, system: str) -> dict:
    """``stage``'s production request (``build_request`` with ``SCHEMAS[stage]``)."""
    return build_request(stage, model=model, system=system, user=user, schema=SCHEMAS[stage])


async def _ask(stage: str, request: dict, *, db_factory, item_ref: str, only_model: bool) -> Answer:
    model = request["model"]
    # The reference is asked alone: an Opus 5 refusal leaves the item out of the agreement
    # figures (audit M2), so a fallback answer would only cost money.
    skip = frozenset(
        m for m in get_settings().landscape_fallback_models if m != model
    ) if only_model else frozenset()
    result = await call_sync(
        stage, request, db_factory=db_factory, item_ref=item_ref, skip_models=skip
    )
    refused_after = result.stop_reason == "refusal"
    answered = result.error is None and not refused_after and result.stop_reason != "max_tokens"
    usage = result.usage or {}
    cost = cost_for_request(
        result.served_by or model,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read=int(usage.get("cache_read_input_tokens") or 0),
        cache_creation=int(usage.get("cache_creation_input_tokens") or 0),
        batch=True,
    )
    return Answer(
        parsed=screen.parse_output(result.text) if answered else None,
        refused_before=refused_after or (result.served_by or model) != model,
        refused_after=refused_after,
        served_by=result.served_by or model,
        cost=cost or Decimal(0),
    )


async def ask_all(
    stage: str, requests: dict[str, dict], *, db_factory, ref_prefix: str,
    only_model: bool = False,
) -> dict[str, Answer]:
    """Send each request synchronously (``call_sync``, the refusal chain included unless
    ``only_model``), a few at a time; the answers by the requests' keys."""
    gate = asyncio.Semaphore(_CONCURRENCY)

    async def one(key: str, request: dict) -> tuple[str, Answer]:
        async with gate:
            return key, await _ask(
                stage, request, db_factory=db_factory, item_ref=f"{ref_prefix}:{key}",
                only_model=only_model,
            )

    return dict(await asyncio.gather(*(one(k, r) for k, r in requests.items())))


def total_cost(answers: Iterable[Answer]) -> Decimal:
    return sum((a.cost for a in answers), Decimal(0))


# --- metrics (spec §10.1) ---------------------------------------------------------------------


def _share(hits: int, total: int, *, empty: float = 1.0) -> float:
    return hits / total if total else empty


def screen_kept(answer: Answer) -> bool:
    return answer.parsed is not None and answer.parsed.get("in_scope") in KEPT


def screen_metrics(scored: list[tuple[bool, Answer]], every: list[Answer]) -> dict:
    """S2 against the reference: ``scored`` pairs whether the reference kept the document with
    the candidate's answer; ``every`` holds all the candidate's answers, reference-refused items
    included, for the refusal rates (before and after the fallback chain)."""
    refs = sum(1 for kept, _ in scored if kept)
    picked = sum(1 for _, answer in scored if screen_kept(answer))
    hits = sum(1 for kept, answer in scored if kept and screen_kept(answer))
    refusal = _share(sum(a.refused_before for a in every), len(every), empty=0.0)
    recall = _share(hits, refs)
    precision = _share(hits, picked, empty=1.0 if not refs else 0.0)
    checks = {
        "refusal": refusal <= SCREEN_MAX_REFUSAL,
        "recall": recall >= SCREEN_MIN_RECALL,
        "precision": precision >= SCREEN_MIN_PRECISION,
    }
    return {
        "items": len(scored),
        "refusal_rate": refusal,
        "refusal_rate_after_fallback": _share(
            sum(a.refused_after for a in every), len(every), empty=0.0
        ),
        "recall": recall,
        "precision": precision,
        "checks": checks,
        "passed": all(checks.values()),
        "quality": recall + precision - refusal,
    }


@dataclass(frozen=True)
class Extraction:
    """One S3 answer as S3 stores it: quote-checked and normalized (``extract._prepare``)."""

    candidates: list[dict]
    keys: frozenset[tuple[str, str, str]]
    verified: int
    dropped: int


def facet_key(candidate: dict) -> tuple[str, str, str]:
    """What S3 agreement compares after normalization (§10.1): the targets, the direction and
    the disease, by the identities the idea key uses (HGNC / Mondo ids, else the provisional
    or folded name)."""
    return (
        ideas._target_ids(candidate.get("targets")),
        ideas._direction(candidate),
        ideas._disease_id(candidate.get("disease")),
    )


async def prepare_extraction(
    parsed: dict | None, doc: LandscapeDocument, criteria: Criteria
) -> Extraction | None:
    """``parsed`` quote-checked against ``doc``'s text and normalized with ``criteria``; None
    when it is not an extraction."""
    if parsed is None or not isinstance(parsed.get("ideas"), list):
        return None
    prepared = await extract._prepare(parsed, doc, criteria)
    return Extraction(
        candidates=prepared.candidates,
        keys=frozenset(facet_key(c) for c in prepared.candidates),
        verified=prepared.verified,
        dropped=prepared.dropped,
    )


def extract_metrics(pairs: list[tuple[frozenset, Extraction | None]]) -> dict:
    """S3 against the reference: per document, the reference's facet keys and the candidate's
    extraction. Agreement is the shared keys over all keys either side named, summed over the
    documents, so a missed idea and an invented one both count against it."""
    shared = union = verified = checked = 0
    for expected, got in pairs:
        keys = got.keys if got is not None else frozenset()
        shared += len(expected & keys)
        union += len(expected | keys)
        if got is not None:
            verified += got.verified
            checked += got.verified + got.dropped
    agreement, quotes = _share(shared, union), _share(verified, checked)
    checks = {"agreement": agreement >= EXTRACT_MIN_AGREEMENT, "quotes": quotes >= EXTRACT_MIN_QUOTES}
    return {
        "items": len(pairs),
        "agreement": agreement,
        "quote_verification": quotes,
        "checks": checks,
        "passed": all(checks.values()),
        "quality": agreement + quotes,
    }


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def gate_score_metrics(
    pairs: list[tuple[dict, dict | None]], *, gates_min: float, scores_min: float
) -> dict:
    """Gates and scores against expected outputs (S4, S6, S9; §10.1, §10.4).

    Each expected output is ``{"gating", "scores"}`` or ``{"kept": True}`` (a positive control
    or a rescue, which must not be excluded). Gates agree when every expected gate is matched;
    each scored dimension's share within ±1 is computed on its own, and the lowest is the one
    held to ``scores_min``."""
    gate_hits = 0
    within: dict[str, int] = defaultdict(int)
    totals: dict[str, int] = defaultdict(int)
    for expected, got in pairs:
        if expected.get("kept"):
            gate_hits += got is not None and excluded_reason(got) is None
            continue
        gating = (got or {}).get("gating") or {}
        gate_hits += got is not None and all(
            gating.get(key) == value for key, value in (expected.get("gating") or {}).items()
        )
        scores = (got or {}).get("scores") or {}
        for key, value in (expected.get("scores") or {}).items():
            totals[key] += 1
            other = scores.get(key)
            within[key] += _number(value) and _number(other) and abs(other - value) <= 1
    gates = _share(gate_hits, len(pairs))
    score_share = min((within[k] / totals[k] for k in totals), default=1.0)
    checks = {"gates": gates >= gates_min, "scores": score_share >= scores_min}
    return {
        "items": len(pairs),
        "gate_agreement": gates,
        "score_within_one": score_share,
        "checks": checks,
        "passed": all(checks.values()),
        "quality": gates + score_share,
    }


def triage_expected(parsed: dict | None) -> dict | None:
    """The stored reference of an S4 answer, or None when it has no gating or no integer
    score for each S4 dimension."""
    if parsed is None or not isinstance(parsed.get("gating"), dict):
        return None
    scores = parsed.get("scores") or {}
    if not all(_number(scores.get(k)) for k in TRIAGE_DIMENSIONS):
        return None
    return {"gating": parsed["gating"], "scores": {k: scores[k] for k in TRIAGE_DIMENSIONS}}


# --- choosing (D43) ---------------------------------------------------------------------------


def pick(results: dict[str, dict], order: tuple[str, ...]) -> tuple[str, bool]:
    """``(model, passed)``: the cheapest passing candidate (a cost tie goes to the earlier one in
    ``order``), else the best-scoring one, by thresholds met and then quality (D43)."""
    passing = [m for m in order if results[m]["passed"]]
    if passing:
        return min(passing, key=lambda m: (Decimal(results[m]["cost_usd"]), order.index(m))), True
    best = max(
        order,
        key=lambda m: (sum(results[m]["checks"].values()), results[m]["quality"], -order.index(m)),
    )
    return best, False


# --- the sample (D68) ---------------------------------------------------------------------------


def _stratum(doc: LandscapeDocument) -> tuple[str, str, int]:
    """(OpenAlex primary topic, kind, year); a document without one is ``unknown`` / year 0."""
    extra = (doc.details or {}).get("extra") or {}
    topic = extra.get("primary_topic") if isinstance(extra, dict) else None
    year = doc.published_on.year if doc.published_on else 0
    return (topic or "unknown", doc.kind, year)


def stratified_sample(
    items: list, stratum: Callable[[Any], Hashable], n: int, rng: random.Random
) -> list:
    """``n`` of ``items`` (all when fewer), each stratum's share proportional to its size, the
    remainder going to the largest fractions; random within each stratum."""
    if n >= len(items):
        return list(items)
    groups: dict[Hashable, list] = defaultdict(list)
    for item in items:
        groups[stratum(item)].append(item)
    keys = sorted(groups, key=str)
    for key in keys:
        rng.shuffle(groups[key])
    quotas = {key: n * len(groups[key]) / len(items) for key in keys}
    take = {key: int(quotas[key]) for key in keys}
    by_fraction = sorted(keys, key=lambda k: (take[k] - quotas[k], str(k)))
    for key in by_fraction[: n - sum(take.values())]:
        take[key] += 1
    return [item for key in keys for item in groups[key][: take[key]]]


async def judging_epoch_id(db: AsyncSession) -> uuid.UUID:
    """The epoch whose prompts the measurements use: the newest unactivated one (the pilot's
    candidate), else the active one. ``LookupError`` when there is neither."""
    epoch_id = await db.scalar(
        select(LandscapeEpoch.id)
        .where(LandscapeEpoch.status.in_(("candidate", "held")))
        .order_by(LandscapeEpoch.created_at.desc(), LandscapeEpoch.id)
        .limit(1)
    )
    if epoch_id is None:
        active = await epochs.active_epoch(db)
        epoch_id = active.id if active is not None else None
    if epoch_id is None:
        raise LookupError("no epoch to judge with: none is a candidate, held or active")
    return epoch_id


async def _pilot_documents(db: AsyncSession) -> list[LandscapeDocument]:
    """Every S1-eligible document that is not a tombstone. The bake-off runs in phase
    ``pilot``, when these are the pilot intake and the positive controls (D68)."""
    D = LandscapeDocument
    return list(
        (
            await db.scalars(
                select(D)
                .where(D.s1_status == "eligible", D.merged_into.is_(None))
                .order_by(D.created_at, D.id)
            )
        ).all()
    )


# --- the three rounds ---------------------------------------------------------------------------


@dataclass
class _Round:
    """One stage's outcome: the report section and the regression items it seeds."""

    summary: dict
    items: list[dict]


def _round_summary(results: dict[str, dict], order: tuple[str, ...], **extra: Any) -> dict:
    model, passed = pick(results, order)
    return {
        "candidates": results, "pick": model, "passed": passed, "shortfall": not passed, **extra,
    }




def _refusals(answers: Iterable[Answer]) -> dict:
    answers = list(answers)
    return {
        "refusal_rate": _share(sum(a.refused_before for a in answers), len(answers), empty=0.0),
        "refusal_rate_after_fallback": _share(
            sum(a.refused_after for a in answers), len(answers), empty=0.0
        ),
        "cost_usd": str(total_cost(answers)),
    }


async def _answers(
    stage: str, users: dict[str, str], *, inputs: EpochInputs, model: str, db_factory,
    reference: bool = False,
) -> dict[str, Answer]:
    """Each user message sent to ``model`` with the epoch's ``stage`` prompt; the reference
    model alone, without the fallback chain."""
    requests = {
        key: stage_request(stage, user, model=model, system=inputs.system(stage))
        for key, user in users.items()
    }
    return await ask_all(
        stage, requests, db_factory=db_factory,
        ref_prefix="bakeoff:reference" if reference else f"bakeoff:{model}",
        only_model=reference,
    )


async def _screen_round(
    db_factory, inputs: EpochInputs, docs: list[LandscapeDocument], order: tuple[str, ...]
) -> tuple[_Round, list[LandscapeDocument]]:
    """S2 on ``docs``; also returns the documents the reference kept."""
    users = {str(doc.id): screen.user_message(doc) for doc in docs}
    reference = await _answers(
        "screen", users, inputs=inputs, model=REFERENCE_MODEL, db_factory=db_factory,
        reference=True,
    )
    usable = [
        key for key in users
        if reference[key].parsed is not None
        and reference[key].parsed.get("in_scope") in (*KEPT, "no")
    ]
    results = {}
    for model in order:
        answers = await _answers("screen", users, inputs=inputs, model=model, db_factory=db_factory)
        scored = [(screen_kept(reference[key]), answers[key]) for key in usable]
        results[model] = {
            **screen_metrics(scored, list(answers.values())),
            "cost_usd": str(total_cost(answers.values())),
        }
    items = [
        {"item_kind": "screen", "origin": "bakeoff",
         "item_ref": {"document_id": key, "user": users[key]},
         "expected": {"decision": reference[key].parsed["in_scope"]}}
        for key in usable
    ]
    kept = [doc for doc in docs if str(doc.id) in usable and screen_kept(reference[str(doc.id)])]
    summary = _round_summary(
        results, order, sample=len(docs),
        reference_refused=sum(a.refused_before for a in reference.values()),
        reference_unusable=len(users) - len(usable),
    )
    return _Round(summary, items), kept


async def _extract_round(
    db_factory, inputs: EpochInputs, kept: list[LandscapeDocument], n: int,
    rng: random.Random, order: tuple[str, ...],
) -> tuple[_Round, dict[str, tuple[LandscapeDocument, Extraction]]]:
    """S3: the reference on every kept document (the S4 pool), the candidates on a stratified
    ``n`` of them. Also returns the reference's extraction of each kept document."""
    docs = {str(doc.id): doc for doc in kept}
    users = {key: screen.user_message(doc) for key, doc in docs.items()}
    reference_answers = await _answers(
        "extract", users, inputs=inputs, model=REFERENCE_MODEL, db_factory=db_factory,
        reference=True,
    )
    reference = {
        key: await prepare_extraction(reference_answers[key].parsed, docs[key], inputs.criteria)
        for key in docs
    }
    usable = [doc for doc in kept if reference[str(doc.id)] is not None]
    sample = [str(doc.id) for doc in stratified_sample(usable, _stratum, n, rng)]
    results = {}
    for model in order:
        answers = await _answers(
            "extract", {key: users[key] for key in sample}, inputs=inputs, model=model,
            db_factory=db_factory,
        )
        got = {
            key: await prepare_extraction(answers[key].parsed, docs[key], inputs.criteria)
            for key in sample
        }
        results[model] = {
            **extract_metrics([(reference[key].keys, got[key]) for key in sample]),
            **_refusals(answers.values()),
        }
    items = [
        {"item_kind": "extract", "origin": "bakeoff",
         "item_ref": {"document_id": key, "user": users[key]},
         "expected": {"keys": sorted(list(k) for k in reference[key].keys)}}
        for key in sample
    ]
    summary = _round_summary(
        results, order, sample=len(sample), pool=len(usable),
        reference_refused=sum(a.refused_before for a in reference_answers.values()),
        reference_unusable=len(docs) - len(usable),
    )
    pool = {key: (docs[key], ex) for key, ex in reference.items() if ex is not None}
    return _Round(summary, items), pool


def _document_fields(doc: LandscapeDocument) -> dict:
    """The only document fields the blinded S4 dossier lists (D20)."""
    return {
        "kind": doc.kind,
        "document_type": doc.document_type,
        "date": doc.published_on.isoformat() if doc.published_on else None,
    }


def _modalities(members: list[dict]) -> list[str]:
    out: list[str] = []
    for candidate in members:
        modality = candidate.get("modality")
        text = modality.get("as_written") if isinstance(modality, dict) else None
        if text and text not in out:
            out.append(text)
    return out


async def _group_dossier(
    db: AsyncSession, members: list[dict], docs_by_id: dict[uuid.UUID, LandscapeDocument]
) -> str:
    """One in-memory idea's S4 dossier: the first candidate's facets (as ``merge_candidate``
    keeps them), its evidence from ``ideas.evidence_payload`` (a failed source is shown as
    unavailable) and the D35 document list."""
    first = members[0]
    docs = list({c["document_id"]: docs_by_id[c["document_id"]] for c in members}.values())
    listed = ideas.dossier_documents(None, docs)
    listed_ids = {doc.id for doc in listed}
    fields = {
        "idea_type": first.get("idea_type"), "targets": first.get("targets"),
        "direction": first.get("direction"), "agent": first.get("agent"),
        "disease": first.get("disease"), "modality_variants": _modalities(members),
        "stage": first.get("stage"), "evidence_grade": first.get("evidence_grade"),
        "jhu_group_count": ideas.group_counts(None, docs)[0], "document_count": len(docs),
        "candidates": [c for c in members if c["document_id"] in listed_ids],
    }
    evidence = await ideas.evidence_payload(
        db, targets=list(first.get("targets") or []), disease=first.get("disease"),
        agent=first.get("agent"),
    )
    errors = evidence.get("errors") or {}
    status = {
        source: {"status": "failed" if source in errors else "done"}
        for source in ideas.EVIDENCE_SOURCES
    }
    return triage_dossier(
        fields, {**evidence, "evidence_status": status}, [_document_fields(d) for d in listed]
    )


async def _dossiers(
    db_factory, pool: dict[str, tuple[LandscapeDocument, Extraction]]
) -> list[tuple[tuple, str]]:
    """``(stratum, dossier)`` per idea the reference's extractions merge into, in memory."""
    docs_by_id = {doc.id: doc for doc, _ in pool.values()}
    candidates = [
        {**candidate, "document_id": doc.id}
        for doc, extraction in pool.values() for candidate in extraction.candidates
    ]
    groups = ideas.group_candidates(
        candidates, senior_labs={doc_id: doc.lab_key for doc_id, doc in docs_by_id.items()}
    )
    out = []
    async with db_factory() as db:
        for members in groups.values():
            dossier = await _group_dossier(db, members, docs_by_id)
            await db.commit()  # the evidence-cache rows evidence_payload added
            out.append((_stratum(docs_by_id[members[0]["document_id"]]), dossier))
    return out


async def _triage_round(
    db_factory, inputs: EpochInputs, pool: dict[str, tuple[LandscapeDocument, Extraction]],
    n: int, rng: random.Random, order: tuple[str, ...],
) -> _Round:
    """S4 on a stratified ``n`` of the in-memory ideas."""
    sample = stratified_sample(await _dossiers(db_factory, pool), lambda d: d[0], n, rng)
    users = {str(index): dossier for index, (_, dossier) in enumerate(sample)}
    reference_answers = await _answers(
        "triage", users, inputs=inputs, model=REFERENCE_MODEL, db_factory=db_factory,
        reference=True,
    )
    expected = {key: triage_expected(a.parsed) for key, a in reference_answers.items()}
    usable = [key for key in users if expected[key] is not None]
    results = {}
    for model in order:
        answers = await _answers("triage", users, inputs=inputs, model=model, db_factory=db_factory)
        results[model] = {
            **gate_score_metrics(
                [(expected[key], answers[key].parsed) for key in usable],
                gates_min=TRIAGE_MIN_GATES, scores_min=TRIAGE_MIN_SCORES,
            ),
            **_refusals(answers.values()),
        }
    items = [
        {"item_kind": "triage", "origin": "bakeoff", "item_ref": {"user": users[key]},
         "expected": expected[key]}
        for key in usable
    ]
    summary = _round_summary(
        results, order, sample=len(users),
        reference_refused=sum(a.refused_before for a in reference_answers.values()),
        reference_unusable=len(users) - len(usable),
    )
    return _Round(summary, items)


async def new_set_version(db: AsyncSession, *, notes: str) -> int:
    """Add the next ``LandscapeRegressionSet`` version in the caller's transaction (flushed,
    not committed) and return it. Its ``created_at`` is the database's wall clock
    (``clock_timestamp()``), not the transaction start, so which version was current when a
    candidate was created is well defined (§10.4)."""
    latest = await db.scalar(select(func.max(LandscapeRegressionSet.version)))
    version = (latest or 0) + 1
    db.add(LandscapeRegressionSet(
        version=version, notes=notes, created_at=func.clock_timestamp()
    ))
    await db.flush()
    return version


async def run_bakeoff(
    db_factory, *, sample_sizes=(600, 60, 60), candidates=CANDIDATE_MODELS,
) -> uuid.UUID:
    """Run the Phase 0 bake-off (spec §10.1; see the module docstring); returns the id of the
    ``LandscapeReport("bakeoff")`` it stores.

    ``sample_sizes`` are the S2, S3 and S4 sample sizes. Per stage the report holds each
    candidate's metrics (refusal rates before and after the fallback chain, cost), how many
    items the reference refused (left out of the agreement figures), the pick and whether it
    passed. With no passing candidate the best one is picked and ``shortfall`` is set (D43).
    ``recommended_settings`` maps each stage's environment variable to its pick. The items the
    reference answered, with its outputs, seed a new regression-set version. ``LookupError``
    when no epoch exists to take the prompts from.
    """
    n_screen, n_extract, n_triage = sample_sizes
    order = tuple(candidates)
    seed = random.SystemRandom().randrange(2**62)
    rng = random.Random(seed)
    async with db_factory() as db:
        epoch_id = await judging_epoch_id(db)
        inputs = await epochs.epoch_inputs(db, epoch_id)
        pilot = await _pilot_documents(db)
    s2, kept = await _screen_round(
        db_factory, inputs, stratified_sample(pilot, _stratum, n_screen, rng), order
    )
    s3, pool = await _extract_round(db_factory, inputs, kept, n_extract, rng, order)
    s4 = await _triage_round(db_factory, inputs, pool, n_triage, rng, order)
    rounds = {"screen": s2, "extract": s3, "triage": s4}
    report_id = uuid.uuid4()
    async with db_factory() as db:
        version = await new_set_version(db, notes="bake-off references (spec §10.4)")
        db.add_all([
            LandscapeRegressionItem(set_version=version, **item)
            for stage_round in rounds.values() for item in stage_round.items
        ])
        db.add(LandscapeReport(
            id=report_id, kind="bakeoff", created_at=func.clock_timestamp(),
            payload={
                "epoch_id": str(epoch_id),
                "seed": seed,
                "reference_model": REFERENCE_MODEL,
                "candidates": list(order),
                "pilot_documents": len(pilot),
                "stages": {stage: r.summary for stage, r in rounds.items()},
                "recommended_settings": {
                    STAGE_SPECS[stage].model_setting.upper(): r.summary["pick"]
                    for stage, r in rounds.items()
                },
                "shortfall": [stage for stage, r in rounds.items() if r.summary["shortfall"]],
                "regression_set_version": version,
            },
        ))
        await db.commit()
    return report_id


# --- S4 repeatability (spec §10.2) ----------------------------------------------------------------


def _same_call(first: dict | None, second: dict | None) -> bool:
    """Identical gates and every S4 score within ±1."""
    if not isinstance(first, dict) or not isinstance(second, dict):
        return False
    if (first.get("gating") or {}) != (second.get("gating") or {}):
        return False
    a, b = first.get("scores") or {}, second.get("scores") or {}
    return all(
        _number(a.get(k)) and _number(b.get(k)) and abs(a[k] - b[k]) <= 1
        for k in TRIAGE_DIMENSIONS
    )


async def run_s4_repeatability(db_factory, *, n: int = 50) -> dict:
    """§10.2: ``n`` pilot ideas, drawn at random from the judging epoch's S4 rows with a stored
    dossier, each scored twice by that epoch's S4 model (analysis B7).

    Returns ``{"epoch_id", "model", "ideas", "consistent", "share"}``: ``share`` is the share
    of ideas whose two answers have identical gates and every score within ±1 (an idea with a
    missing answer counts against it), None when there is no idea. Writes only ledger rows.
    """
    T = LandscapeTriage
    async with db_factory() as db:
        epoch_id = await judging_epoch_id(db)
        inputs = await epochs.epoch_inputs(db, epoch_id)
        dossiers = (
            await db.scalars(
                select(T.raw["dossier"].astext)
                .where(T.epoch_id == epoch_id, T.raw["dossier"].astext.is_not(None))
                .order_by(func.random())
                .limit(n)
            )
        ).all()
    model = inputs.model("triage")
    users = {f"{i}:{turn}": d for i, d in enumerate(dossiers) for turn in (1, 2)}
    answers = await _answers("triage", users, inputs=inputs, model=model, db_factory=db_factory)
    consistent = sum(
        _same_call(answers[f"{i}:1"].parsed, answers[f"{i}:2"].parsed)
        for i in range(len(dossiers))
    )
    return {
        "epoch_id": str(epoch_id),
        "model": model,
        "ideas": len(dossiers),
        "consistent": consistent,
        "share": consistent / len(dossiers) if dossiers else None,
    }
