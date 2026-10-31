"""S5, the cut (spec §7.6; L7, L23, L56, D10, D11, D34, D37, D52, X2).

A cut ranks the epoch's pool on the §7.6 keys (``scoring.tie_key``, with the ladders of the
epoch's own criteria, §7.11), folds near-duplicates (D34), keeps K = ceil(N ×
``landscape_cut_fraction``) of the N unfolded ideas (1% by default, §9.1, L7), and queues the
top K for S6. When ranks K and K+1 are equal on keys 1-5, their whole tie class is ordered by
one batched pairwise tournament over the blinded S4 dossiers (``comparator``), with no S6 and
no synchronous call; until it ends the previous cut stays current.

``landscape_cuts`` as this module writes it:

- ``ordered_idea_ids``: the unfolded ideas in rank order, as id strings; the first ``k`` are
  the cut. While ``boundary_pending`` it holds the order before the tie-break.
- ``folds``: ``{"folded": {keeper id: [folded ids, in rank order]}}``; when ranks K and K+1
  tied, also ``"tie_class": {"ids": [...], "versions": {id: triage version}}``, the class in
  its keys 6-8 order (contiguous in ``ordered_idea_ids``) and the versions its comparisons
  are keyed on.
- An epoch's current cut is its newest ``complete`` one (``completed_at``);
  ``LandscapeIdea.in_current_cut`` marks that cut's top K.
"""
from __future__ import annotations

import logging
import math
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import Any

from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.landscaping import comparator, epochs
from src.landscaping.comparator import IN_FLIGHT, tournament_pairs, win_fraction_order
from src.landscaping.evidence.mondo import is_ancestor
from src.landscaping.scoring import tie_key
from src.models.landscape import (
    LandscapeComparison,
    LandscapeCut,
    LandscapeDeep,
    LandscapeDocument,
    LandscapeEpoch,
    LandscapeExtraction,
    LandscapeIdea,
    LandscapeIdeaEvidence,
    LandscapeReviewBatch,
    LandscapeTriage,
)

logger = logging.getLogger(__name__)

#: The cut runs by this time of the UTC day even while S4 work is pending (spec §7.1).
CUT_DEADLINE = timedelta(hours=6)
#: The boundary tournament is a full round robin up to this class size, else 10-regular (F8).
BOUNDARY_FULL_UP_TO = 20
#: §7.6 keys 1-5 decide whether ranks K and K+1 tie; keys 6-8 order the class's ties.
_BOUNDARY_KEYS = 5
#: The batch cycle before the first review batch is issued (spec §7.1: a week).
_DEFAULT_BATCH_CYCLE = timedelta(days=7)
_RANKED_STATUSES = ("done", "rescued")
_DEEP_EXCLUDED = "excluded:deep_gate"


def cut_due(*, now: datetime, s4_pending: int, last_cut_at: datetime | None) -> bool:
    """Whether the epoch's daily cut is due (spec §7.1): no cut yet this UTC day, and either no
    S4 work of the cycle is pending or it is 06:00 UTC or later, whichever comes first.

    ``now`` and ``last_cut_at`` are timezone-aware. ``s4_pending`` should count only triage
    rows of each idea's current version: a ``pending`` row of an older version is never
    submitted, and counting it would hold every cut to 06:00.
    """
    day_start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    if last_cut_at is not None and last_cut_at >= day_start:
        return False
    return s4_pending == 0 or now >= day_start + CUT_DEADLINE


# --- the pool -------------------------------------------------------------------------------


@dataclass
class _Ranked:
    idea_id: uuid.UUID
    key: tuple
    idea_type: str
    targets: frozenset[str]
    agent: str | None
    direction: str | None
    #: Mondo id, else provisional key: the disease's identity for folding.
    disease: str | None
    mondo: str | None
    rank: int = 0


def _facet_id(value: Any, *keys: str) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in keys:
        found = value.get(key)
        if isinstance(found, str) and found.strip():
            return found.strip().casefold()
    return None


def _target_ids(targets: Any) -> frozenset[str]:
    ids = (
        _facet_id(t, "hgnc_id", "provisional_key", "symbol", "as_written")
        for t in (targets or ()) if isinstance(t, dict)
    )
    return frozenset(i for i in ids if i)


def _decided(status: str) -> bool:
    return status in _RANKED_STATUSES or status.startswith("excluded:")


def _ranked_triage(current_version: str | None, rows: list) -> Any | None:
    """The S4 row an idea is ranked on: its current version's when decided, else the newest
    decided row of an earlier version (a new version waiting for S4 keeps its place, §7.6).
    None unless that row passed (``done``) or was rescued (§10.5)."""
    chosen = next(
        (r for r in rows if r.idea_version == current_version and _decided(r.status)), None
    )
    if chosen is None:
        earlier = [r for r in rows if r.idea_version != current_version and _decided(r.status)]
        chosen = max(earlier, key=lambda r: r.created_at, default=None)
    return chosen if chosen is not None and chosen.status in _RANKED_STATUSES else None


async def _triage_rows(db: AsyncSession, epoch_id: uuid.UUID) -> dict[uuid.UUID, list]:
    t = LandscapeTriage
    rows = (
        await db.execute(
            select(t.idea_id, t.idea_version, t.status, t.triage_score, t.evidence_count,
                   t.evidence_grade, t.stage, t.created_at)
            .where(t.epoch_id == epoch_id, t.status.not_in(IN_FLIGHT))
        )
    ).all()
    by_idea: dict[uuid.UUID, list] = defaultdict(list)
    for row in rows:
        by_idea[row.idea_id].append(row)
    return by_idea


async def _deep_excluded(db: AsyncSession, epoch_id: uuid.UUID) -> set[tuple[uuid.UUID, str]]:
    """(idea, version) pairs excluded at S6 and not rescued (D11)."""
    rows = (
        await db.execute(
            select(LandscapeDeep.idea_id, LandscapeDeep.idea_version, LandscapeDeep.status)
            .where(
                LandscapeDeep.epoch_id == epoch_id,
                LandscapeDeep.status.in_((_DEEP_EXCLUDED, "rescued")),
            )
        )
    ).all()
    excluded = {(i, v) for i, v, s in rows if s == _DEEP_EXCLUDED}
    return excluded - {(i, v) for i, v, s in rows if s == "rescued"}


async def _qualifying_documents(db: AsyncSession, epoch_id: uuid.UUID) -> dict[uuid.UUID, tuple]:
    """Per idea, (distinct qualifying documents, newest publication date): S1-eligible
    idea-source documents that are not tombstones."""
    doc = LandscapeDocument
    rows = (
        await db.execute(
            select(LandscapeIdeaEvidence.idea_id, func.count(doc.id.distinct()),
                   func.max(doc.published_on))
            .join(LandscapeExtraction,
                  LandscapeExtraction.id == LandscapeIdeaEvidence.extraction_id)
            .join(doc, doc.id == LandscapeExtraction.document_id)
            .where(
                LandscapeExtraction.epoch_id == epoch_id,
                LandscapeExtraction.basis == "summary",
                doc.s1_status == "eligible",
                doc.merged_into.is_(None),
            )
            .group_by(LandscapeIdeaEvidence.idea_id)
        )
    ).all()
    return {idea_id: (count, newest) for idea_id, count, newest in rows}


async def _pool(db: AsyncSession, epoch_id: uuid.UUID) -> list[_Ranked]:
    """The §7.6 pool in rank order: ideas with a qualifying document and a passed or rescued
    S4 result, less versions excluded at S6 and not rescued, ranked on the epoch's own criteria
    ladders (§7.11)."""
    criteria = (await epochs.epoch_inputs(db, epoch_id)).criteria
    i = LandscapeIdea
    ideas = (
        await db.execute(
            select(i.id, i.version_hash, i.idea_type, i.targets, i.agent, i.direction,
                   i.disease, i.stage, i.evidence_grade, i.jhu_group_count)
            .where(i.epoch_id == epoch_id)
        )
    ).all()
    triage = await _triage_rows(db, epoch_id)
    excluded = await _deep_excluded(db, epoch_id)
    documents = await _qualifying_documents(db, epoch_id)
    ranked: list[_Ranked] = []
    for idea in ideas:
        row = _ranked_triage(idea.version_hash, triage.get(idea.id, []))
        count, newest = documents.get(idea.id, (0, None))
        if row is None or count == 0:
            continue
        if (idea.id, idea.version_hash) in excluded or (idea.id, row.idea_version) in excluded:
            continue
        key = tie_key({
            "idea_id": idea.id, "triage_score": row.triage_score,
            "evidence_count": row.evidence_count,
            "evidence_grade": row.evidence_grade or idea.evidence_grade,
            "jhu_group_count": idea.jhu_group_count, "stage": row.stage or idea.stage,
            "document_count": count, "newest_document_date": newest,
        }, criteria=criteria)
        ranked.append(_Ranked(
            idea_id=idea.id, key=key, idea_type=idea.idea_type,
            targets=_target_ids(idea.targets),
            agent=_facet_id(idea.agent, "chembl_id", "provisional_key", "as_written", "name"),
            direction=idea.direction,
            disease=_facet_id(idea.disease, "mondo_id", "provisional_key"),
            mondo=(idea.disease or {}).get("mondo_id") if isinstance(idea.disease, dict) else None,
        ))
    ranked.sort(key=lambda r: r.key)
    for n, item in enumerate(ranked):
        item.rank = n
    return ranked


# --- folds, K and the boundary ---------------------------------------------------------------


def _facet_key(item: _Ranked) -> tuple | None:
    facet = item.targets or (frozenset({item.agent}) if item.agent else None)
    return (item.idea_type, facet, item.direction) if facet else None


def _related_diseases(a: str | None, b: str | None) -> bool:
    return bool(a and b and a != b and (is_ancestor(a, b) or is_ancestor(b, a)))


def _keeper(item: _Ranked, by_facet: dict, by_disease: dict) -> _Ranked | None:
    """The highest-ranked kept idea ``item`` folds under (D34), or None."""
    found: list[_Ranked] = []
    facet = _facet_key(item)
    if facet is not None and item.mondo:
        match = next(
            (k for k in by_facet.get(facet, ()) if _related_diseases(k.mondo, item.mondo)), None
        )
        if match is not None:
            found.append(match)
    if item.targets and item.disease:
        match = next(
            (k for k in by_disease.get((item.direction, item.disease), ())
             if k.targets <= item.targets or item.targets <= k.targets),
            None,
        )
        if match is not None:
            found.append(match)
    return min(found, key=lambda k: k.rank, default=None)


def _fold(ranked: list[_Ranked]) -> tuple[list[_Ranked], dict[str, list[str]]]:
    """Near-duplicates fold under a higher-ranked kept idea (D34): the same type, targets (or
    agent) and direction with Mondo ancestor and descendant diseases; or nested target sets
    with the same direction and disease. Returns the kept ideas and keeper -> folded ids."""
    kept: list[_Ranked] = []
    folded: dict[str, list[str]] = {}
    by_facet: dict[tuple, list[_Ranked]] = defaultdict(list)
    by_disease: dict[tuple, list[_Ranked]] = defaultdict(list)
    for item in ranked:
        keeper = _keeper(item, by_facet, by_disease)
        if keeper is not None:
            folded.setdefault(str(keeper.idea_id), []).append(str(item.idea_id))
            continue
        kept.append(item)
        facet = _facet_key(item)
        if facet is not None:
            by_facet[facet].append(item)
        if item.targets and item.disease:
            by_disease[(item.direction, item.disease)].append(item)
    return kept, folded


def _k(n: int) -> int:
    """K = ceil(n × ``landscape_cut_fraction``) (§9.1, L7), computed exactly: the setting is
    read as its decimal text, so ``ceil(100 × 0.07)`` is 7, not 8."""
    return math.ceil(n * Fraction(str(get_settings().landscape_cut_fraction)))


def _tie_span(kept: list[_Ranked], k: int) -> tuple[int, int] | None:
    """The [start, end) slice of the class equal to ranks K and K+1 on keys 1-5, if they tie."""
    if k == 0 or k >= len(kept):
        return None
    boundary = kept[k - 1].key[:_BOUNDARY_KEYS]
    if kept[k].key[:_BOUNDARY_KEYS] != boundary:
        return None
    start, end = k - 1, k + 1
    while start > 0 and kept[start - 1].key[:_BOUNDARY_KEYS] == boundary:
        start -= 1
    while end < len(kept) and kept[end].key[:_BOUNDARY_KEYS] == boundary:
        end += 1
    return start, end


def _seed(cut_id: uuid.UUID) -> int:
    """The cut's draw seed, so its pairs can be drawn again from the stored class."""
    return cut_id.int & ((1 << 63) - 1)


def _resolved(
    order: list[uuid.UUID], members: list[uuid.UUID], rows: list[LandscapeComparison]
) -> list[uuid.UUID]:
    """``order`` with the tie class reordered by win fraction; equal fractions keep their
    keys 6-8 order (their position in ``order``)."""
    position = {idea_id: n for n, idea_id in enumerate(order)}
    resolved = win_fraction_order(comparator.pair_results(rows), tiebreak=position.__getitem__)
    resolved += [m for m in members if m not in resolved]
    start = position[members[0]]
    return order[:start] + resolved + order[start + len(members):]


# --- completing a cut -----------------------------------------------------------------------


async def _batch_cycle_start(db: AsyncSession) -> datetime:
    """When the current batch cycle began: the newest review batch's issue, else a week ago."""
    latest = await db.scalar(select(func.max(LandscapeReviewBatch.created_at)))
    if latest is not None:
        return latest
    return await db.scalar(select(func.now() - _DEFAULT_BATCH_CYCLE))


async def _queue_deep(
    db: AsyncSession, cut: LandscapeCut, top: list[uuid.UUID], previous: LandscapeCut | None
) -> None:
    """Queue S6 for each top-K idea with no S6 row at its current version (§7.6 Entry). An
    idea already in the previous cut whose version changed is re-queued at most once per
    batch cycle (D37): not while it has an S6 row made since the cycle began."""
    if not top:
        return
    prev_top = set((previous.ordered_idea_ids or [])[:previous.k]) if previous else set()
    versions = dict(
        (await db.execute(
            select(LandscapeIdea.id, LandscapeIdea.version_hash)
            .where(LandscapeIdea.id.in_(top))
        )).all()
    )
    made: dict[uuid.UUID, list[tuple[str, datetime]]] = defaultdict(list)
    for idea_id, version, created_at in (
        await db.execute(
            select(LandscapeDeep.idea_id, LandscapeDeep.idea_version, LandscapeDeep.created_at)
            .where(LandscapeDeep.epoch_id == cut.epoch_id, LandscapeDeep.idea_id.in_(top))
        )
    ).all():
        made[idea_id].append((version, created_at))
    cycle_start = await _batch_cycle_start(db)
    values = []
    for idea_id in top:
        version, earlier = versions.get(idea_id), made[idea_id]
        if version is None or any(v == version for v, _ in earlier):
            continue
        if earlier and str(idea_id) in prev_top and any(c >= cycle_start for _, c in earlier):
            continue
        values.append({"id": uuid.uuid4(), "idea_id": idea_id, "idea_version": version,
                       "epoch_id": cut.epoch_id, "cut_id": cut.id})
    if values:
        await db.execute(
            pg_insert(LandscapeDeep).values(values)
            .on_conflict_do_nothing(constraint="uq_landscape_deep_idea_version_epoch")
        )


async def _complete(db: AsyncSession, cut: LandscapeCut, order: list[uuid.UUID]) -> None:
    """Make ``cut`` the epoch's current cut: store its final order, mark its top K
    ``in_current_cut`` (an idea leaving keeps its results, §7.6 Leaving) and queue S6."""
    previous = await db.scalar(
        select(LandscapeCut)
        .where(
            LandscapeCut.epoch_id == cut.epoch_id, LandscapeCut.status == "complete",
            LandscapeCut.id != cut.id,
        )
        .order_by(LandscapeCut.completed_at.desc().nulls_last(), LandscapeCut.created_at.desc())
        .limit(1)
    )
    cut.ordered_idea_ids = [str(idea_id) for idea_id in order]
    cut.status = "complete"
    cut.completed_at = func.clock_timestamp()
    top = order[:cut.k]
    await db.execute(
        update(LandscapeIdea)
        .where(
            LandscapeIdea.epoch_id == cut.epoch_id, LandscapeIdea.in_current_cut.is_(True),
            LandscapeIdea.id.not_in(top),
        )
        .values(in_current_cut=False)
    )
    if top:
        await db.execute(
            update(LandscapeIdea).where(LandscapeIdea.id.in_(top)).values(in_current_cut=True)
        )
    await _queue_deep(db, cut, top, previous)
    logger.info(
        "cut %s: N=%d K=%d, tie class %d", cut.id, cut.n, cut.k, cut.tie_class_size
    )


def _new_cut(
    cut_id: uuid.UUID, epoch_id: uuid.UUID, *, status: str, order: list[uuid.UUID],
    folds: dict, k: int, class_size: int,
) -> LandscapeCut:
    return LandscapeCut(
        id=cut_id, epoch_id=epoch_id, status=status, n=len(order), k=k,
        ordered_idea_ids=[str(idea_id) for idea_id in order], folds=folds,
        tie_class_size=class_size, created_at=func.clock_timestamp(),
    )


async def _class_rows(db: AsyncSession, cut: LandscapeCut) -> tuple[list, list]:
    """(class ids, comparison rows) of a cut's tie class, drawn again from its stored seed."""
    info = (cut.folds or {}).get("tie_class") or {}
    members = [uuid.UUID(i) for i in info.get("ids") or []]
    versions = {uuid.UUID(i): v for i, v in (info.get("versions") or {}).items()}
    pairs = tournament_pairs(members, full_up_to=BOUNDARY_FULL_UP_TO, seed=_seed(cut.id))
    rows = await comparator.tournament_rows(
        db, epoch_id=cut.epoch_id, pairs=pairs, versions=versions, basis="triage"
    )
    return members, rows


async def _finish(db_factory, cut_id: uuid.UUID) -> uuid.UUID | None:
    """Complete a ``boundary_pending`` cut once its class is settled; its id, or None while
    an answer is still owed."""
    async with db_factory() as db:
        cut = await db.get(LandscapeCut, cut_id, with_for_update=True, populate_existing=True)
        if cut is None:
            return None
        if cut.status == "complete":
            return cut.id
        members, rows = await _class_rows(db, cut)
        if not comparator.settled(rows):
            await db.commit()
            return None
        order = [uuid.UUID(i) for i in cut.ordered_idea_ids or []]
        await _complete(db, cut, _resolved(order, members, rows))
        await db.commit()
        return cut.id


async def _set_batch(db_factory, cut_id: uuid.UUID, batch_id: uuid.UUID) -> None:
    async with db_factory() as db:
        await db.execute(
            update(LandscapeCut).where(LandscapeCut.id == cut_id)
            .values(boundary_batch_id=batch_id)
        )
        await db.commit()


async def _advance_pending(db_factory, cut_id: uuid.UUID, *, may_submit: bool) -> None:
    """A cut due while the boundary tournament runs is skipped (§7.6). Meanwhile: finish the
    pending cut once settled, and (when `may_submit`) send the comparisons still ``pending``:
    ones a result put back, or ones held while over the daily cap."""
    async with db_factory() as db:
        cut = await db.get(LandscapeCut, cut_id)
        _, rows = await _class_rows(db, cut)
        await db.commit()
    if comparator.settled(rows):
        await _finish(db_factory, cut_id)
        return
    if not may_submit:
        return
    resend = [row.id for row in rows if row.status == "pending" and row.fallback_from is None]
    if resend:
        batch_id = await comparator.submit_rows(db_factory, resend)
        if batch_id is not None:
            await _set_batch(db_factory, cut_id, batch_id)


async def _frozen(db: AsyncSession, epoch: LandscapeEpoch) -> bool:
    """A candidate with a complete cut taken after its drain takes no further cut until it
    activates (spec §7.6, §7.11 step 4)."""
    if epoch.status not in ("candidate", "held") or epoch.drained_at is None:
        return False
    return bool(await db.scalar(select(exists().where(
        LandscapeCut.epoch_id == epoch.id, LandscapeCut.status == "complete",
        LandscapeCut.created_at > epoch.drained_at,
    ))))


async def _boundary(
    db_factory, cut_id: uuid.UUID, epoch_id: uuid.UUID, order: list[uuid.UUID],
    folds: dict, k: int, span: tuple[int, int], *, may_submit: bool,
) -> uuid.UUID | None:
    """Resolve the tie class across K: from cached answers when every comparison is already
    settled (no call), else by one Message Batch, leaving the cut ``boundary_pending``. Without
    `may_submit` the comparisons stay ``pending`` and a later call sends them."""
    members = order[span[0]:span[1]]
    pairs = tournament_pairs(members, full_up_to=BOUNDARY_FULL_UP_TO, seed=_seed(cut_id))
    async with db_factory() as db:
        versions = await comparator.usable_versions(
            db, epoch_id=epoch_id, ids=members, basis="triage"
        )
        rows = await comparator.tournament_rows(
            db, epoch_id=epoch_id, pairs=pairs, versions=versions, basis="triage"
        )
        folds = {**folds, "tie_class": {
            "ids": [str(m) for m in members],
            "versions": {str(m): versions[m] for m in members},
        }}
        if comparator.settled(rows):
            cut = _new_cut(cut_id, epoch_id, status="complete", order=order, folds=folds,
                           k=k, class_size=len(members))
            db.add(cut)
            await db.flush()
            await _complete(db, cut, _resolved(order, members, rows))
            await db.commit()
            return cut_id
        db.add(_new_cut(cut_id, epoch_id, status="boundary_pending", order=order, folds=folds,
                        k=k, class_size=len(members)))
        await db.commit()
    if not may_submit:
        return None
    batch_id = await comparator.submit_rows(db_factory, [row.id for row in rows])
    if batch_id is not None:
        await _set_batch(db_factory, cut_id, batch_id)
    return None


async def run_cut(
    db_factory, epoch_id: uuid.UUID, *, may_submit: bool = True,
) -> uuid.UUID | None:
    """Take S5 for ``epoch_id`` (spec §7.6); the id of the new complete cut, or None.

    `may_submit` false (the worker over its daily cap) still takes the cut but sends no
    boundary comparison: the tie class waits ``pending`` in a ``boundary_pending`` cut until a
    call with `may_submit` sends it (the cap defers model work only, §12.2).

    None when: the epoch is gone or retired; a boundary tournament is pending (one cut per
    epoch in flight: this call only advances it, finishing it once settled); the epoch is a
    candidate frozen by a complete post-drain cut; or this cut's tie class across K went to a
    Message Batch (``finish_cut`` completes it; the previous cut stays current until then).
    Slots lost to S6 exclusions are not refilled within a cut; the next cut's pool drops them.
    """
    async with db_factory() as db:
        epoch = await db.get(LandscapeEpoch, epoch_id)
        if epoch is None or epoch.status == "retired":
            return None
        pending_id = await db.scalar(
            select(LandscapeCut.id)
            .where(LandscapeCut.epoch_id == epoch_id, LandscapeCut.status == "boundary_pending")
            .limit(1)
        )
        if pending_id is None:
            if await _frozen(db, epoch):
                return None
            ranked = await _pool(db, epoch_id)
    if pending_id is not None:
        await _advance_pending(db_factory, pending_id, may_submit=may_submit)
        return None
    kept, folded = _fold(ranked)
    order = [item.idea_id for item in kept]
    k = _k(len(kept))
    folds: dict = {"folded": folded}
    cut_id = uuid.uuid4()
    span = _tie_span(kept, k)
    if span is not None:
        return await _boundary(
            db_factory, cut_id, epoch_id, order, folds, k, span, may_submit=may_submit,
        )
    async with db_factory() as db:
        cut = _new_cut(cut_id, epoch_id, status="complete", order=order, folds=folds, k=k,
                       class_size=0)
        db.add(cut)
        await db.flush()
        await _complete(db, cut, order)
        await db.commit()
    return cut_id


async def finish_cut(
    db_factory, *, epoch_id: uuid.UUID, tournament_batch_id: uuid.UUID
) -> uuid.UUID:
    """Complete the epoch's ``boundary_pending`` cut from its tournament's answers; returns
    the cut id. Call once ``tournament_complete`` is true for ``tournament_batch_id``.

    Idempotent: with no pending cut it returns the epoch's newest complete cut.
    ``LookupError`` when the epoch has no cut; ``RuntimeError`` when the class still owes an
    answer.
    """
    async with db_factory() as db:
        pending_id = await db.scalar(
            select(LandscapeCut.id)
            .where(LandscapeCut.epoch_id == epoch_id, LandscapeCut.status == "boundary_pending")
            .limit(1)
        )
        if pending_id is None:
            done = await db.scalar(
                select(LandscapeCut.id)
                .where(LandscapeCut.epoch_id == epoch_id, LandscapeCut.status == "complete")
                .order_by(LandscapeCut.completed_at.desc().nulls_last())
                .limit(1)
            )
            if done is None:
                raise LookupError(f"epoch {epoch_id} has no cut for batch {tournament_batch_id}")
            return done
    finished = await _finish(db_factory, pending_id)
    if finished is None:
        raise RuntimeError(f"cut {pending_id}: its boundary tournament still owes answers")
    return finished
