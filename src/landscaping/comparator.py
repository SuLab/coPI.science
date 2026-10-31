"""Pairwise comparisons of ideas: the S5 boundary tie-break and S7 (spec §7.6, §7.8; D13, D52,
F8, X2).

Every answer is one ``landscape_comparisons`` row, keyed on (idea a, version a, idea b,
version b, epoch, basis, ask order), so a later cut or ranking that needs the same pair at the
same versions reuses it and makes no call (§7.8). A pair is stored canonically, ``idea_a``
being the lower id; ``ask_order`` 1 shows ``idea_a`` as A, 2 shows ``idea_b`` as A. Each pair
is asked in both orders.

- Basis ``triage``: the blinded S4 dossier stored on each triage row (``raw["dossier"]``), for
  the S5 boundary tournament. It goes through a Message Batch, never a synchronous call, so it
  never waits on a live simulation run (§7.6).
- Basis ``deep``: the S6 record, for S7: ``compare`` (one synchronous insertion question) and
  the rebuild tournament (a Message Batch). S6 output restates untrusted source text, so it
  is fenced with ``delimit(..., "publication_abstract")``; the S4 dossier fences its own
  extracted text (``triage.triage_dossier``) and is shown as stored.

Every request is built with the comparison's epoch's own system prompt and model
(``epochs.epoch_inputs``), never the current files and settings, so an epoch never mixes
judgments (spec §7.11). A refused batch item goes down the D42 chain through ``FALLBACKS``,
whose ``build`` rebuilds the request from the database.
"""
from __future__ import annotations

import json
import logging
import random
import uuid
from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable, Sequence
from fractions import Fraction
from itertools import combinations
from typing import Any, Literal

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.prompt_safety import delimit
from src.landscaping import batches, epochs
from src.landscaping.batches import BatchOutcome, FallbackAdapter
from src.landscaping.llm_client import CallResult, build_request, call_sync
from src.landscaping.schemas import SCHEMAS
from src.models.landscape import (
    LandscapeBatchItem,
    LandscapeComparison,
    LandscapeDeep,
    LandscapeIdea,
    LandscapeTriage,
)
from src.services.blackbird_rubric import weighted_score
from src.services.job_queue import job_error_text
from src.services.json_extract import extract_json

logger = logging.getLogger(__name__)

STAGE = "rerank"
Basis = Literal["triage", "deep"]
#: Stage-row statuses that still owe an answer.
IN_FLIGHT = ("pending", "submitting", "submitted", "running")
#: A triage or deep row whose result can be compared.
_USABLE = ("done", "rescued")
#: Rows per INSERT or ``IN`` list, well under asyncpg's 32,767 parameters.
_CHUNK = 1000
_MISSING_TEXT = "No record is stored for this idea."


def _chunks(values: Sequence, size: int = _CHUNK) -> Iterable[Sequence]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


# --- the draw and the order -----------------------------------------------------------------


def tournament_pairs(ids: list, *, full_up_to: int, degree: int = 10, seed: int) -> list[tuple]:
    """The pairs of a tournament over ``ids``, each to be asked in both orders (F8).

    Every pair when ``len(ids) <= full_up_to`` (S5: 20; S7 rebuild: 11) or when there are too
    few ideas for ``degree``. Otherwise a balanced ``degree``-regular draw: ``ids`` shuffled
    with ``random.Random(seed)``, then position i paired with i+1 … i+degree//2 (mod n), so
    every idea meets exactly ``degree`` distinct others (``degree`` even) and one seed always
    gives one draw (spec §7.6, §7.8).
    """
    ids = list(ids)
    if len(set(ids)) != len(ids):
        raise ValueError("tournament ids must be distinct")
    n = len(ids)
    if n <= full_up_to or n <= degree:
        return list(combinations(ids, 2))
    order = list(ids)
    random.Random(seed).shuffle(order)
    return [
        (order[i], order[(i + step) % n])
        for i in range(n)
        for step in range(1, degree // 2 + 1)
    ]


def win_fraction_order(
    results: Iterable[tuple[Hashable, Hashable, Sequence[Hashable | None]]],
    *, tiebreak: Callable[[Any], Any],
) -> list:
    """The ideas of ``results``, strongest first, by win fraction (spec §7.6, §7.8).

    ``results`` holds one ``(a, b, winners)`` entry per pair met, ``winners`` being the winner
    of each answered order (``pair_results``). Two answers that agree give the pair to that
    idea; answers that disagree make a tie, half a win each; a single answer decides alone; a
    pair with no answer is not counted as met. Win fraction = (wins + 0.5 × ties) / pairs met,
    or 1/2 for an idea none of whose pairs was answered. Equal fractions fall to ``tiebreak``,
    an ascending sort key on the idea (S5: its keys 6-8 position; S7: S6 provisional
    ``weighted_score``, then id).
    """
    wins: dict[Any, Fraction] = defaultdict(Fraction)
    met: dict[Any, int] = defaultdict(int)
    ideas: dict[Any, None] = {}
    for a, b, winners in results:
        ideas.setdefault(a)
        ideas.setdefault(b)
        answers = {w for w in winners if w in (a, b)}
        if not answers:
            continue
        met[a] += 1
        met[b] += 1
        if len(answers) == 1:
            wins[answers.pop()] += 1
        else:
            wins[a] += Fraction(1, 2)
            wins[b] += Fraction(1, 2)

    def fraction(idea: Any) -> Fraction:
        return wins[idea] / met[idea] if met[idea] else Fraction(1, 2)

    return sorted(ideas, key=lambda idea: (-fraction(idea), tiebreak(idea)))


def pair_results(rows: Iterable[LandscapeComparison]) -> list[tuple[uuid.UUID, uuid.UUID, list]]:
    """``win_fraction_order`` input from comparison rows: one entry per canonical pair, with
    the winner of each ``done`` order."""
    grouped: dict[tuple[uuid.UUID, uuid.UUID], list] = {}
    for row in rows:
        winners = grouped.setdefault((row.idea_a, row.idea_b), [])
        if row.status == "done" and row.winner_idea_id is not None:
            winners.append(row.winner_idea_id)
    return [(a, b, winners) for (a, b), winners in grouped.items()]


def settled(rows: Iterable[LandscapeComparison]) -> bool:
    """True when no row still owes an answer (refused and failed rows count as settled)."""
    return all(row.status not in IN_FLIGHT for row in rows)


# --- rows -----------------------------------------------------------------------------------


def _stage_model(basis: str) -> type:
    if basis == "triage":
        return LandscapeTriage
    if basis == "deep":
        return LandscapeDeep
    raise ValueError(f"unknown comparison basis {basis!r}")


async def usable_versions(
    db: AsyncSession, *, epoch_id: uuid.UUID, ids: Iterable[uuid.UUID], basis: str
) -> dict[uuid.UUID, str]:
    """Each idea's version to compare on: its current version when that version's ``basis``
    row is ``done`` or ``rescued``, else the newest such row of an earlier version (an idea
    whose new version still waits keeps its previous result, §7.6). Ideas with none are left
    out."""
    model = _stage_model(basis)
    best: dict[uuid.UUID, tuple[tuple, str]] = {}
    for chunk in _chunks(list(ids)):
        rows = (
            await db.execute(
                select(model.idea_id, model.idea_version, model.created_at,
                       LandscapeIdea.version_hash)
                .join(LandscapeIdea, LandscapeIdea.id == model.idea_id)
                .where(
                    model.epoch_id == epoch_id, model.idea_id.in_(chunk),
                    model.status.in_(_USABLE),
                )
            )
        ).all()
        for idea_id, version, created_at, current in rows:
            rank = (version == current, created_at)
            if idea_id not in best or rank > best[idea_id][0]:
                best[idea_id] = (rank, version)
    return {idea_id: version for idea_id, (_, version) in best.items()}


def _canonical(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    return (a, b) if a < b else (b, a)


async def tournament_rows(
    db: AsyncSession, *, epoch_id: uuid.UUID, pairs: Iterable[tuple],
    versions: dict[uuid.UUID, str], basis: str,
) -> list[LandscapeComparison]:
    """Both orders of every pair at ``versions``, created ``pending`` where absent (insert
    ignoring the cache key) and read back fresh. The caller commits."""
    wanted: dict[tuple, None] = {}
    for a, b in pairs:
        x, y = _canonical(a, b)
        for order in (1, 2):
            wanted.setdefault((x, versions[x], y, versions[y], order))
    values = [
        {"id": uuid.uuid4(), "idea_a": x, "version_a": vx, "idea_b": y, "version_b": vy,
         "epoch_id": epoch_id, "basis": basis, "ask_order": order}
        for x, vx, y, vy, order in wanted
    ]
    for chunk in _chunks(values):
        await db.execute(
            pg_insert(LandscapeComparison).values(list(chunk))
            .on_conflict_do_nothing(constraint="uq_landscape_comparisons_pair")
        )
    ideas = sorted({key[0] for key in wanted} | {key[2] for key in wanted})
    found: list[LandscapeComparison] = []
    for chunk in _chunks(ideas):
        found += (
            await db.scalars(
                select(LandscapeComparison)
                .where(
                    LandscapeComparison.epoch_id == epoch_id,
                    LandscapeComparison.basis == basis,
                    LandscapeComparison.idea_a.in_(chunk),
                    LandscapeComparison.idea_b.in_(ideas),
                )
                .execution_options(populate_existing=True)
            )
        ).all()
    return [
        row for row in found
        if (row.idea_a, row.version_a, row.idea_b, row.version_b, row.ask_order) in wanted
    ]


# --- texts and requests ---------------------------------------------------------------------


def deep_text(deep: LandscapeDeep) -> str:
    """What the S7 comparator reads of an S6 record: its free-text ``summary`` when S6 wrote
    one, else its judged fields as JSON."""
    if deep.summary and deep.summary.strip():
        return deep.summary
    fields = {
        "gating": deep.gating,
        "gating_rationales": deep.gating_rationales,
        "provisional_scores": deep.provisional_scores,
        "dimension_rationales": deep.dimension_rationales,
        "disease_profile": deep.disease_profile,
        "target_biology": deep.target_biology,
        "flags_reviewed": deep.flags_reviewed,
        "outside_group_count": deep.outside_group_count,
    }
    shown = {key: value for key, value in fields.items() if value is not None}
    return json.dumps(shown, indent=1, sort_keys=True, default=str)


async def _texts(
    db: AsyncSession, rows: Iterable[LandscapeComparison]
) -> dict[tuple[uuid.UUID, str], str]:
    """``(idea id, version) -> text`` for both ideas of every row: the triage row's stored
    dossier for basis ``triage``, ``deep_text`` fenced as ``publication_abstract`` for
    ``deep``. The regression gate stores these texts and replays them through ``_request``,
    so its requests match production's (D32)."""
    wanted: dict[tuple[uuid.UUID, str], set[tuple[uuid.UUID, str]]] = defaultdict(set)
    for row in rows:
        wanted[(row.epoch_id, row.basis)] |= {(row.idea_a, row.version_a), (row.idea_b, row.version_b)}
    texts: dict[tuple[uuid.UUID, str], str] = {}
    for (epoch_id, basis), keys in wanted.items():
        model = _stage_model(basis)
        for chunk in _chunks(sorted({idea_id for idea_id, _ in keys})):
            records = (
                await db.scalars(
                    select(model).where(model.epoch_id == epoch_id, model.idea_id.in_(chunk))
                )
            ).all()
            for record in records:
                key = (record.idea_id, record.idea_version)
                if key not in keys:
                    continue
                text = (
                    (record.raw or {}).get("dossier") if basis == "triage"
                    else delimit(deep_text(record), "publication_abstract")
                )
                if text:
                    texts[key] = text
        for key in keys - texts.keys():
            logger.warning("comparison: no %s text for idea %s version %s", basis, *key)
            texts[key] = _MISSING_TEXT
    return texts


def _shown(row: LandscapeComparison) -> tuple[uuid.UUID, uuid.UUID]:
    """(idea shown as A, idea shown as B)."""
    return (row.idea_a, row.idea_b) if row.ask_order == 1 else (row.idea_b, row.idea_a)


def _shown_texts(
    row: LandscapeComparison, texts: dict[tuple[uuid.UUID, str], str]
) -> tuple[str, str]:
    version = {row.idea_a: row.version_a, row.idea_b: row.version_b}
    first, second = _shown(row)
    return texts[(first, version[first])], texts[(second, version[second])]


async def _inputs(db: AsyncSession, epoch_id: uuid.UUID) -> tuple[str, str]:
    """(system prompt, model) of the rerank stage in ``epoch_id``'s own inputs (§7.11)."""
    inputs = await epochs.epoch_inputs(db, epoch_id)
    return inputs.system(STAGE), inputs.model(STAGE)


def _request(first: str, second: str, *, system: str, model: str, retry: bool) -> dict:
    user = f"# Idea A\n\n{first}\n\n# Idea B\n\n{second}"
    return build_request(
        STAGE, model=model, system=system, user=user, schema=SCHEMAS[STAGE], retry=retry,
    )


def _answer_values(text: str, row: LandscapeComparison) -> dict:
    """Row values for a reply: ``done`` with the winning idea, or ``failed:unparseable``
    (terminal) with the error stored."""
    try:
        parsed = extract_json(text)
        winner = parsed.get("winner") if isinstance(parsed, dict) else None
        if winner not in ("A", "B"):
            raise ValueError(f"rerank winner is not A or B: {winner!r}")
    except ValueError as exc:
        return {"status": "failed:unparseable", "error": job_error_text(exc)}
    first, second = _shown(row)
    reason = parsed.get("reason")
    return {
        "status": "done",
        "winner_idea_id": first if winner == "A" else second,
        "reason": reason if isinstance(reason, str) else None,
    }


def _message_text(message: Any) -> str:
    return "".join(
        block.text for block in (getattr(message, "content", None) or [])
        if getattr(block, "type", None) == "text"
    )


# --- batched tournaments --------------------------------------------------------------------


async def submit_rows(db_factory, row_ids: Iterable[uuid.UUID]) -> uuid.UUID | None:
    """Submit the given comparison rows that are ``pending`` and not waiting for the fallback
    chain as one Message Batch; the batch row id, or None when none qualifies. Used for a new
    tournament and for re-sending rows an errored, expired, canceled or truncated result put
    back to ``pending``. The rows must share one epoch (one model per batch)."""
    async with db_factory() as db:
        rows: list[LandscapeComparison] = []
        for chunk in _chunks(list(row_ids)):
            rows += (
                await db.scalars(
                    select(LandscapeComparison).where(
                        LandscapeComparison.id.in_(chunk),
                        LandscapeComparison.status == "pending",
                        LandscapeComparison.fallback_from.is_(None),
                    )
                )
            ).all()
        if not rows:
            return None
        texts = await _texts(db, rows)
        inputs = {
            epoch_id: await _inputs(db, epoch_id) for epoch_id in {row.epoch_id for row in rows}
        }
    items = []
    for row in rows:
        system, model = inputs[row.epoch_id]
        items.append((row.id, _request(
            *_shown_texts(row, texts), system=system, model=model, retry=row.truncated_once,
        )))
    try:
        return await batches.submit(
            db_factory, stage=STAGE, row_model=LandscapeComparison, items=items
        )
    except ValueError:
        logger.info("rerank: no comparison still pending at submission")
        return None


async def submit_tournament(
    db_factory, *, epoch_id: uuid.UUID, ids: list[uuid.UUID], pairs: list[tuple],
    basis: Basis,
) -> uuid.UUID:
    """One Message Batch asking every uncached pair of ``pairs`` in both orders, at each
    idea's ``usable_versions`` version. Returns the ``landscape_batches`` row id.

    ``ValueError`` when an idea has no ``basis`` result, or when nothing is left to send
    (every comparison cached or already in flight).
    """
    async with db_factory() as db:
        versions = await usable_versions(db, epoch_id=epoch_id, ids=ids, basis=basis)
        missing = [idea_id for idea_id in ids if idea_id not in versions]
        if missing:
            raise ValueError(f"no {basis} result to compare for {len(missing)} idea(s)")
        rows = await tournament_rows(
            db, epoch_id=epoch_id, pairs=pairs, versions=versions, basis=basis
        )
        await db.commit()
    batch_id = await submit_rows(db_factory, [row.id for row in rows])
    if batch_id is None:
        raise ValueError("nothing to submit: every comparison is cached or in flight")
    return batch_id


async def ingest_tournament_outcomes(db_factory, outcomes: list[BatchOutcome]) -> None:
    """Apply one chunk of rerank outcomes through ``batches.apply_result`` (compare-and-set),
    so a replayed chunk changes nothing. ``succeeded`` stores the winner; other kinds take
    ``batches.transition`` (a refusal leaves the row for ``FALLBACKS``)."""
    async with db_factory() as db:
        for outcome in outcomes:
            row = await db.get(LandscapeComparison, outcome.row_id)
            if row is None:
                continue
            if outcome.kind == "succeeded":
                values = _answer_values(_message_text(outcome.message), row)
                values["served_by_model"] = outcome.model
            else:
                values = batches.transition(
                    outcome.kind, attempts=row.attempts, truncated_once=row.truncated_once,
                    model=outcome.model,
                )
            await batches.apply_result(
                db, LandscapeComparison, row_id=outcome.row_id,
                batch_item_id=outcome.batch_item_id, values=values,
            )
        await db.commit()


def _owed(epoch_id: uuid.UUID, basis: str) -> Any:
    """Rows of a batched tournament still owing an answer: in a batch, or ``pending`` after
    one (a resubmission or the fallback chain). A ``pending`` row that was never batched is
    an insertion's (``compare``) and is not a tournament's."""
    c = LandscapeComparison
    return and_(
        c.epoch_id == epoch_id, c.basis == basis,
        or_(
            c.status.in_(("submitting", "submitted")),
            and_(c.status == "pending",
                 or_(c.batch_item_id.is_not(None), c.fallback_from.is_not(None))),
        ),
    )


async def tournament_complete(db: AsyncSession, batch_row_id: uuid.UUID) -> bool:
    """Whether the tournament of a rerank batch has every answer it will get: no comparison
    of its epoch and basis is still in a batch, waiting to be re-sent, or waiting for the
    fallback chain. One tournament per epoch and basis is in flight at a time (§7.6, §7.8), so
    this covers re-sends made under a later batch."""
    keys = (
        await db.execute(
            select(LandscapeComparison.epoch_id, LandscapeComparison.basis)
            .join(LandscapeBatchItem, LandscapeBatchItem.row_id == LandscapeComparison.id)
            .where(LandscapeBatchItem.batch_id == batch_row_id)
            .distinct()
        )
    ).all()
    for epoch_id, basis in keys:
        if await db.scalar(select(exists().where(_owed(epoch_id, basis)))):
            return False
    return True


# --- the fallback chain (D42) ---------------------------------------------------------------


async def _fallback_request(db: AsyncSession, row: LandscapeComparison) -> dict:
    """The row's request rebuilt from the database, in its epoch's inputs (D42)."""
    texts = await _texts(db, [row])
    system, model = await _inputs(db, row.epoch_id)
    return _request(
        *_shown_texts(row, texts), system=system, model=model, retry=row.truncated_once
    )


async def _fallback_success(
    db: AsyncSession, row: LandscapeComparison, result: CallResult
) -> None:
    for column, value in _answer_values(result.text, row).items():
        setattr(row, column, value)


FALLBACKS: tuple[FallbackAdapter, ...] = (
    FallbackAdapter(STAGE, LandscapeComparison, _fallback_request, _fallback_success),
)


# --- synchronous S7 insertion ---------------------------------------------------------------


def _sync_values(result: CallResult, row: LandscapeComparison) -> dict:
    if result.error is not None:
        return {"status": "failed:errored", "error": job_error_text(result.error)}
    if result.stop_reason == "refusal":
        return {"status": "refused", "refusal_category": result.refusal_category}
    if result.stop_reason == "max_tokens":
        return {"status": "failed:truncated", "served_by_model": result.served_by}
    return {**_answer_values(result.text, row), "served_by_model": result.served_by}


async def _ask(
    db_factory, row: LandscapeComparison, texts: dict[tuple[uuid.UUID, str], str],
    *, system: str, model: str,
) -> uuid.UUID | None:
    """One order asked synchronously (the client's refusal chain included); the winner, or
    None when the order ends without an answer. The outcome is terminal: a re-cut never
    asks the same order again."""
    request = _request(
        *_shown_texts(row, texts), system=system, model=model, retry=row.truncated_once
    )
    result = await call_sync(
        STAGE, request, db_factory=db_factory,
        item_ref=f"{LandscapeComparison.__tablename__}:{row.id}",
    )
    values = _sync_values(result, row)
    async with db_factory() as db:
        await db.execute(
            update(LandscapeComparison)
            .where(LandscapeComparison.id == row.id, LandscapeComparison.status == "pending")
            .values(**values, updated_at=func.now())
        )
        await db.commit()
    return values.get("winner_idea_id")


async def _deep_scores(
    db: AsyncSession, epoch_id: uuid.UUID, versions: dict[uuid.UUID, str]
) -> dict[uuid.UUID, float]:
    """Each idea's S6 provisional weighted score at its compared version (§7.8)."""
    records = (
        await db.scalars(
            select(LandscapeDeep).where(
                LandscapeDeep.epoch_id == epoch_id, LandscapeDeep.idea_id.in_(list(versions))
            )
        )
    ).all()
    scores: dict[uuid.UUID, float] = {}
    for record in records:
        if versions.get(record.idea_id) != record.idea_version:
            continue
        score = record.provisional_weighted_score
        scores[record.idea_id] = (
            score if score is not None else weighted_score(record.provisional_scores)
        )
    return scores


def _decide(
    a: uuid.UUID, b: uuid.UUID, winners: list, scores: dict[uuid.UUID, float]
) -> int:
    answers = {w for w in winners if w in (a, b)}
    if len(answers) == 1:
        winner = answers.pop()
    else:
        winner = min((a, b), key=lambda i: (-scores.get(i, float("-inf")), i))
    return -1 if winner == a else 1


async def compare(
    db_factory, a: uuid.UUID, b: uuid.UUID, *, epoch_id: uuid.UUID, basis: Literal["deep"]
) -> int:
    """The S7 insertion comparator (spec §7.8): -1 when idea ``a`` ranks above ``b``, 1 when
    ``b`` does; never 0.

    Both orders are asked synchronously over the two S6 records, each only when not already
    answered at these versions (cached across cuts, so a re-cut makes no call). When the
    answers agree, or only one order was answered, that idea wins; otherwise the higher S6
    provisional ``weighted_score`` wins, then the lower id. ``ValueError`` when either idea
    has no usable S6 record in the epoch. The caller serializes insertions and asks nothing
    while a simulation run is live (D33).
    """
    if basis != "deep":
        raise ValueError("compare is the S7 comparator: basis must be 'deep'")
    async with db_factory() as db:
        versions = await usable_versions(db, epoch_id=epoch_id, ids=[a, b], basis=basis)
        if a not in versions or b not in versions:
            raise ValueError("compare needs a done or rescued S6 record for both ideas")
        rows = await tournament_rows(
            db, epoch_id=epoch_id, pairs=[(a, b)], versions=versions, basis=basis
        )
        texts = await _texts(db, rows)
        scores = await _deep_scores(db, epoch_id, versions)
        system, model = await _inputs(db, epoch_id)
        await db.commit()
    winners: list = []
    for row in sorted(rows, key=lambda r: r.ask_order):
        if row.status == "done":
            winners.append(row.winner_idea_id)
        elif row.status == "pending" and row.fallback_from is None and row.batch_item_id is None:
            winners.append(await _ask(db_factory, row, texts, system=system, model=model))
    return _decide(a, b, winners, scores)
