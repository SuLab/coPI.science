"""S7, the ranked list (spec §7.8; D13, D30, D33, X3, F8).

One ranked list per epoch over the S6-passed ideas of the current cut: the cut's top K whose
S6 record is ``done`` or ``rescued`` (``comparator.usable_versions``), each at that record's
version. ``landscape_rankings.position`` is 1-based and contiguous.

- **Rebuild** when the list is empty, or when more than 25% of the cut is new since the last
  rebuild: one Message Batch of a sampled tournament (``comparator.submit_tournament``, basis
  ``deep``). While its comparisons are owed, ``update_rankings`` only re-sends the ones an
  errored, expired, canceled or truncated result put back to ``pending``; insertions and
  removals wait and no second rebuild is submitted. ``finish_rebuild`` lands it.
- **Insertion** otherwise: each new or changed idea is placed by binary search with the
  synchronous ``comparator.compare``, one idea at a time. A simulation run live before any
  question stops the insertions; the ideas not yet placed wait for the next call (D33).
- **Removal** of an idea that left the cut makes no call; a re-cut of unchanged ideas makes none.

The list has no state table of its own, so three facts are read from the rows (X3):

- *Placed by a rebuild*: an idea has a ``done`` batched (``batch_item_id`` set) deep comparison
  with another member, both at their current versions. "New since the last rebuild" is a member
  not placed by a rebuild, which covers ideas inserted since, changed ones and absent ones.
- *Changed*: a ranking row's ``id`` is the id of the ``landscape_deep`` row it was placed on. A
  row whose id is another S6 record of the same idea was placed on an older version, and is
  removed and inserted again. An epoch copy gives a ranking row the id of its copied S6
  record; a row whose id is no S6 record (its record had no copy) is kept as it is.
- *Rebuild in flight*: the epoch's newest rerank batch of deep comparisons is not
  ``comparator.tournament_complete``.

Every change to the list happens under ``landscape_rerank_lock_key(epoch_id)``, held through a
``SessionAdvisoryLock`` on its own connection for the whole call, binary searches included,
across many commits, so two insertions never compute positions from the same list.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import uuid
from collections.abc import AsyncIterator, Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.landscaping import comparator, liveness
from src.models.landscape import (
    LandscapeBatch,
    LandscapeBatchItem,
    LandscapeComparison,
    LandscapeCut,
    LandscapeDeep,
    LandscapeRanking,
)
from src.services import advisory_locks
from src.services.blackbird_rubric import weighted_score

logger = logging.getLogger(__name__)

BASIS = "deep"
#: A rebuild is a full round robin up to this many ideas, else 10-regular (§7.8, F8).
REBUILD_FULL_UP_TO = 11
#: A rebuild runs when more than 1/REBUILD_NEW_DIVISOR of the cut is new since the last one.
REBUILD_NEW_DIVISOR = 4
#: Seconds between attempts to take the epoch's rerank lock while another call holds it.
LOCK_RETRY_SECONDS = 2.0
_USABLE = ("done", "rescued")


@dataclass(frozen=True)
class _Member:
    """An S6-passed idea of the cut: the S6 record it is ranked on, and that record's
    provisional weighted score (the tie-break, §7.8)."""

    deep_id: uuid.UUID
    version: str
    score: float


# --- the lock -------------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _rerank_lock(epoch_id: uuid.UUID) -> AsyncIterator[advisory_locks.SessionAdvisoryLock]:
    """Hold the epoch's rerank lock, waiting while another call holds it."""
    key = advisory_locks.landscape_rerank_lock_key(epoch_id)
    url = get_settings().database_url
    while True:
        lock = advisory_locks.SessionAdvisoryLock(url, key)
        try:
            held = await lock.acquire()
        except BaseException:
            await lock.release()
            raise
        if held:
            break
        await lock.release()
        await asyncio.sleep(LOCK_RETRY_SECONDS)
    try:
        yield lock
    finally:
        await lock.release()


# --- reads ----------------------------------------------------------------------------------


def _seed(epoch_id: uuid.UUID, ids: Iterable[uuid.UUID]) -> int:
    """The rebuild draw's seed: one member set in one epoch always gives one draw."""
    text = ":".join([str(epoch_id), *sorted(str(i) for i in ids)])
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big") & ((1 << 63) - 1)


def _tiebreak(members: Mapping[uuid.UUID, _Member]) -> Callable[[uuid.UUID], tuple]:
    """Higher S6 provisional weighted score first, then the lower idea id (§7.8)."""
    return lambda idea: (-members[idea].score, idea)


async def _current_cut(db: AsyncSession, epoch_id: uuid.UUID) -> LandscapeCut | None:
    """The epoch's newest complete cut (§7.6)."""
    return await db.scalar(
        select(LandscapeCut)
        .where(LandscapeCut.epoch_id == epoch_id, LandscapeCut.status == "complete")
        .order_by(LandscapeCut.completed_at.desc().nulls_last(), LandscapeCut.created_at.desc())
        .limit(1)
    )


async def _members(
    db: AsyncSession, epoch_id: uuid.UUID, cut: LandscapeCut | None
) -> dict[uuid.UUID, _Member]:
    """The cut's top K with a usable S6 record, each with that record."""
    if cut is None:
        return {}
    ids = [uuid.UUID(str(i)) for i in (cut.ordered_idea_ids or [])[:cut.k]]
    versions = await comparator.usable_versions(db, epoch_id=epoch_id, ids=ids, basis=BASIS)
    if not versions:
        return {}
    d = LandscapeDeep
    rows = (
        await db.execute(
            select(d.id, d.idea_id, d.idea_version, d.provisional_weighted_score,
                   d.provisional_scores)
            .where(d.epoch_id == epoch_id, d.idea_id.in_(list(versions)), d.status.in_(_USABLE))
        )
    ).all()
    members: dict[uuid.UUID, _Member] = {}
    for deep_id, idea_id, version, score, scores in rows:
        if versions.get(idea_id) != version:
            continue
        members[idea_id] = _Member(
            deep_id, version, score if score is not None else weighted_score(scores)
        )
    return members


async def _ranked(db: AsyncSession, epoch_id: uuid.UUID) -> list[LandscapeRanking]:
    return list(
        (
            await db.scalars(
                select(LandscapeRanking)
                .where(LandscapeRanking.epoch_id == epoch_id)
                .order_by(LandscapeRanking.position, LandscapeRanking.idea_id)
            )
        ).all()
    )


async def _stale(
    db: AsyncSession, ranked: list[LandscapeRanking], members: Mapping[uuid.UUID, _Member]
) -> set[uuid.UUID]:
    """Ideas to take off the list: those that left the cut, and those placed on an older S6
    record than the one they are now ranked on."""
    leavers = {r.idea_id for r in ranked if r.idea_id not in members}
    suspects = [
        r.id for r in ranked if r.idea_id in members and r.id != members[r.idea_id].deep_id
    ]
    if not suspects:
        return leavers
    older = set(
        (await db.scalars(select(LandscapeDeep.id).where(LandscapeDeep.id.in_(suspects)))).all()
    )
    return leavers | {r.idea_id for r in ranked if r.id in older}


async def _tournament_results(
    db: AsyncSession, epoch_id: uuid.UUID, members: Mapping[uuid.UUID, _Member]
) -> list[tuple]:
    """``win_fraction_order`` input from every answered batched deep comparison between two
    members at their current versions. Synchronous insertion answers are left out: they
    cluster around each idea's neighbours and would skew win fractions."""
    if len(members) < 2:
        return []
    c = LandscapeComparison
    ids = list(members)
    rows = (
        await db.scalars(
            select(c).where(
                c.epoch_id == epoch_id, c.basis == BASIS, c.status == "done",
                c.batch_item_id.is_not(None), c.idea_a.in_(ids), c.idea_b.in_(ids),
            )
        )
    ).all()
    current = [
        r for r in rows
        if r.version_a == members[r.idea_a].version and r.version_b == members[r.idea_b].version
    ]
    return comparator.pair_results(current)


def _placed(results: Iterable[tuple]) -> set[uuid.UUID]:
    return {idea for a, b, winners in results if winners for idea in (a, b)}


async def newest_rebuild_batch(db: AsyncSession, epoch_id: uuid.UUID) -> uuid.UUID | None:
    """The ``landscape_batches`` id of the epoch's newest rebuild tournament, or None."""
    return await db.scalar(
        select(LandscapeBatch.id)
        .join(LandscapeBatchItem, LandscapeBatchItem.batch_id == LandscapeBatch.id)
        .join(LandscapeComparison, LandscapeComparison.id == LandscapeBatchItem.row_id)
        .where(
            LandscapeBatch.stage == comparator.STAGE,
            LandscapeComparison.epoch_id == epoch_id, LandscapeComparison.basis == BASIS,
        )
        .order_by(LandscapeBatch.seq.desc())
        .limit(1)
    )


async def _rebuild_in_flight(db: AsyncSession, epoch_id: uuid.UUID) -> bool:
    """True while the epoch's newest rebuild batch still owes an answer (in a batch, waiting
    to be re-sent, or waiting for the fallback chain)."""
    batch_id = await newest_rebuild_batch(db, epoch_id)
    return batch_id is not None and not await comparator.tournament_complete(db, batch_id)


async def _resend(db_factory, epoch_id: uuid.UUID) -> None:
    """Re-send the epoch's rebuild comparisons a batch result put back to ``pending``
    (``batches.transition``) or reconciliation released: batched before (``batch_item_id``
    set) and not waiting for the fallback chain. Without this the rebuild never completes."""
    c = LandscapeComparison
    async with db_factory() as db:
        ids = list((await db.scalars(select(c.id).where(
            c.epoch_id == epoch_id, c.basis == BASIS, c.status == "pending",
            c.batch_item_id.is_not(None), c.fallback_from.is_(None),
        ))).all())
    if not ids:
        return
    batch_id = await comparator.submit_rows(db_factory, ids)
    logger.info("rerank %s: re-sent %d rebuild comparison(s) in batch %s",
                epoch_id, len(ids), batch_id)


# --- writes ---------------------------------------------------------------------------------


async def _remove(db: AsyncSession, epoch_id: uuid.UUID, ideas: set[uuid.UUID]) -> None:
    """Delete ``ideas``' rows and renumber the rest 1..n in their order. No call."""
    if not ideas:
        return
    r = LandscapeRanking
    await db.execute(delete(r).where(r.epoch_id == epoch_id, r.idea_id.in_(list(ideas))))
    numbered = (
        select(r.id.label("rid"),
               func.row_number().over(order_by=(r.position, r.idea_id)).label("rn"))
        .where(r.epoch_id == epoch_id)
        .subquery()
    )
    await db.execute(
        update(r).where(r.id == numbered.c.rid).values(position=numbered.c.rn)
        .execution_options(synchronize_session=False)
    )


async def _replace(
    db: AsyncSession, epoch_id: uuid.UUID, order: list[uuid.UUID],
    members: Mapping[uuid.UUID, _Member],
) -> None:
    """Make ``order`` the whole list, each row placed on its member's S6 record."""
    await db.execute(delete(LandscapeRanking).where(LandscapeRanking.epoch_id == epoch_id))
    if order:
        await db.execute(pg_insert(LandscapeRanking).values([
            {"id": members[idea].deep_id, "epoch_id": epoch_id, "idea_id": idea,
             "position": n}
            for n, idea in enumerate(order, 1)
        ]))


async def _insert(
    db_factory, epoch_id: uuid.UUID, idea: uuid.UUID, member: _Member
) -> bool:
    """Place one idea by binary search over the committed list: about log₂K synchronous
    questions. False, with the list untouched, when a simulation run is live before a
    question (S7 yields like S6, D33); True otherwise."""
    async with db_factory() as db:
        order = [r.idea_id for r in await _ranked(db, epoch_id)]
    if idea in order:
        return True
    lo, hi = 0, len(order)
    while lo < hi:
        mid = (lo + hi) // 2
        async with db_factory() as db:
            if await liveness.simulation_live(db):
                return False
        above = await comparator.compare(
            db_factory, idea, order[mid], epoch_id=epoch_id, basis=BASIS
        )
        if above < 0:
            hi = mid
        else:
            lo = mid + 1
    r = LandscapeRanking
    async with db_factory() as db:
        await db.execute(
            update(r).where(r.epoch_id == epoch_id, r.position > lo)
            .values(position=r.position + 1)
            .execution_options(synchronize_session=False)
        )
        db.add(r(id=member.deep_id, epoch_id=epoch_id, idea_id=idea, position=lo + 1))
        await db.commit()
    return True


async def _insert_all(
    db_factory, epoch_id: uuid.UUID, ideas: list[uuid.UUID],
    members: Mapping[uuid.UUID, _Member], lock: advisory_locks.SessionAdvisoryLock,
) -> None:
    """Insert ``ideas`` one at a time; an idea the comparator cannot place (an S6 record gone
    meanwhile) is left for the next call. A live simulation run stops the loop: that idea and
    the rest are left for the next call (D33)."""
    for n, idea in enumerate(ideas):
        await lock.check()
        try:
            inserted = await _insert(db_factory, epoch_id, idea, members[idea])
        except ValueError as exc:
            logger.warning("rerank: idea %s not inserted: %s", idea, exc)
            continue
        if not inserted:
            logger.info("rerank %s: a simulation run is live, %d insertion(s) wait",
                        epoch_id, len(ideas) - n)
            return


async def _land(db_factory, epoch_id: uuid.UUID, lock: advisory_locks.SessionAdvisoryLock) -> None:
    """Order the list by the rebuild's win fractions over the current cut, drop leavers and
    insert the rest one at a time. A list that already holds the rebuild-placed ideas in that
    order only loses its stale rows, so landing the same rebuild twice asks nothing new."""
    async with db_factory() as db:
        members = await _members(db, epoch_id, await _current_cut(db, epoch_id))
        ranked = await _ranked(db, epoch_id)
        results = await _tournament_results(db, epoch_id, members)
        placed = _placed(results)
        order = [
            idea for idea in comparator.win_fraction_order(results, tiebreak=_tiebreak(members))
            if idea in placed
        ]
        stale = await _stale(db, ranked, members)
        kept = [r.idea_id for r in ranked if r.idea_id not in stale]
        if [idea for idea in kept if idea in placed] == order:
            await _remove(db, epoch_id, stale)
        else:
            await _replace(db, epoch_id, order, members)
            kept = order
        await db.commit()
    listed = set(kept)
    entrants = sorted((i for i in members if i not in listed), key=_tiebreak(members))
    logger.info("rerank %s: landed %d ranked, inserting %d", epoch_id, len(kept), len(entrants))
    await _insert_all(db_factory, epoch_id, entrants, members, lock)


# --- entry points ---------------------------------------------------------------------------


async def update_rankings(db_factory, *, epoch_id: uuid.UUID, cut_id: uuid.UUID) -> None:
    """Bring the epoch's S7 list up to the complete cut ``cut_id`` (spec §7.8).

    While a rebuild batch is in flight, only re-sends its comparisons a result put back to
    ``pending`` and changes nothing else. Otherwise removes ideas that left the cut or
    changed, then submits a rebuild when the list is empty or more than a quarter of the cut
    is new since the last rebuild (landing it at once when every pair is already answered),
    else inserts the absent members one at a time until a simulation run is live (D33).
    Waits for the epoch's rerank lock, and blocks through every synchronous question it asks.

    Every path may spend on the model (a batch or synchronous questions), so call it only
    when new model work is allowed: the worker calls it from ``_sync_step``, which runs only
    under the daily cap and with no simulation run live.
    """
    async with _rerank_lock(epoch_id) as lock:
        async with db_factory() as db:
            in_flight = await _rebuild_in_flight(db, epoch_id)
        if in_flight:
            logger.info("rerank %s: rebuild in flight, cut %s waits", epoch_id, cut_id)
            await _resend(db_factory, epoch_id)
            return
        async with db_factory() as db:
            cut = await db.get(LandscapeCut, cut_id)
            if cut is None or cut.epoch_id != epoch_id or cut.status != "complete":
                logger.warning("rerank %s: cut %s is not a complete cut of it", epoch_id, cut_id)
                return
            members = await _members(db, epoch_id, cut)
            ranked = await _ranked(db, epoch_id)
            stale = await _stale(db, ranked, members)
            listed = {r.idea_id for r in ranked if r.idea_id not in stale}
            await _remove(db, epoch_id, stale)
            placed = _placed(await _tournament_results(db, epoch_id, members))
            await db.commit()
        new = [idea for idea in members if idea not in placed]
        if len(members) >= 2 and (
            not listed or len(new) * REBUILD_NEW_DIVISOR > len(members)
        ):
            ids = sorted(members)
            pairs = comparator.tournament_pairs(
                ids, full_up_to=REBUILD_FULL_UP_TO, seed=_seed(epoch_id, ids)
            )
            try:
                batch_id = await comparator.submit_tournament(
                    db_factory, epoch_id=epoch_id, ids=ids, pairs=pairs, basis=BASIS
                )
            except ValueError:
                logger.info("rerank %s: every rebuild pair is answered, landing now", epoch_id)
                await _land(db_factory, epoch_id, lock)
                return
            logger.info(
                "rerank %s: rebuild of %d ideas (%d new) in batch %s",
                epoch_id, len(ids), len(new), batch_id,
            )
            return
        entrants = sorted((i for i in members if i not in listed), key=_tiebreak(members))
        await _insert_all(db_factory, epoch_id, entrants, members, lock)


async def finish_rebuild(db_factory, *, epoch_id: uuid.UUID, batch_id: uuid.UUID) -> None:
    """Land a rebuild once its tournament has every answer it will get (spec §7.8).

    ``batch_id`` is a ``landscape_batches`` row of the rebuild; nothing happens while
    ``comparator.tournament_complete`` is false for it. Otherwise orders the rebuild's ideas
    by ``win_fraction_order`` (ties: S6 provisional ``weighted_score``, then id), removes ideas
    that left the current cut meanwhile, and inserts the ideas that entered meanwhile, or whose
    S6 version changed, one at a time. Waits for the epoch's rerank lock.
    """
    async with _rerank_lock(epoch_id) as lock:
        async with db_factory() as db:
            if not await comparator.tournament_complete(db, batch_id):
                logger.info("rerank %s: batch %s still owes answers", epoch_id, batch_id)
                return
        await _land(db_factory, epoch_id, lock)


def card_order(
    cards: Mapping[Hashable, Iterable[uuid.UUID]], *,
    s7_positions: Mapping[uuid.UUID, int], s5_ranks: Mapping[uuid.UUID, int],
) -> list:
    """Card keys in display order (D30): by the best member idea's S7 position, or its S5 rank
    until S7 has placed it.

    ``cards`` maps a card key to its member idea ids. ``s7_positions`` holds the epoch's
    ``landscape_rankings.position`` values; ``s5_ranks`` the 1-based rank in the current cut's
    ``ordered_idea_ids``. Both are 1-based ranks within the cut and are compared as one number;
    on equal numbers an S7 position goes first. Cards with no ranked member go last; ties fall
    to the card key's text.
    """
    def idea_rank(idea: uuid.UUID) -> tuple[float, int]:
        if idea in s7_positions:
            return (s7_positions[idea], 0)
        if idea in s5_ranks:
            return (s5_ranks[idea], 1)
        return (math.inf, 2)

    def card_rank(item: tuple[Hashable, Iterable[uuid.UUID]]) -> tuple:
        key, ideas = item
        return (min((idea_rank(i) for i in ideas), default=(math.inf, 2)), str(key))

    return [key for key, _ in sorted(cards.items(), key=card_rank)]
