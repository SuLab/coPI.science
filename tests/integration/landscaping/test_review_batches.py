"""Weekly review batches, blind-batch probes and the expert sample against Postgres (spec
§13.1, §10.6, §7.9; L45-L47, L52-L55, D30, D48, D50, D62, D63).

Every session shares one connection whose outer transaction rolls back. Opportunities,
assessments, cuts and batches are written directly; probes are made by ``draw_probes``
through ``opportunities.make_probe``. The criteria file's reason-tag approval is faked so a
batch can be issued. Every name below is invented.
"""
import contextlib
import itertools
import json
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.landscaping import epochs, review_batches
from src.landscaping.stages.opportunities import make_probe
from src.models import AppSetting
from src.models.landscape import (
    LandscapeAssessment,
    LandscapeCut,
    LandscapeDecision,
    LandscapeDeep,
    LandscapeEpoch,
    LandscapeExpertJudgment,
    LandscapeIdea,
    LandscapeOpportunity,
    LandscapeOpportunityVersion,
    LandscapeRanking,
    LandscapeReview,
    LandscapeReviewBatch,
    LandscapeReviewBatchItem,
)
from tests.factories import make_user

pytestmark = pytest.mark.integration

FIRST_WEEK = date(2026, 11, 2)
_counter = itertools.count(1)


def week(n: int) -> date:
    return FIRST_WEEK + timedelta(weeks=n - 1)


@dataclass(frozen=True)
class _Idea:
    id: uuid.UUID
    key: str


@dataclass(frozen=True)
class _Card:
    opp: uuid.UUID
    assessment: uuid.UUID | None


class _ScriptedRng:
    """Shuffles by reversing and samples the first items, so draws are predictable."""

    def shuffle(self, items: list) -> None:
        items.reverse()

    def sample(self, population, n: int) -> list:
        return list(population)[:n]


@pytest_asyncio.fixture
async def db_factory(engine):
    async with engine.connect() as conn:
        trans = await conn.begin()

        @contextlib.asynccontextmanager
        async def factory():
            session = AsyncSession(
                bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            try:
                yield session
            finally:
                await session.close()

        try:
            yield factory
        finally:
            if trans.is_active:
                await trans.rollback()


@pytest.fixture(autouse=True)
def approved(monkeypatch):
    """Reason tags approved unless a test says otherwise."""
    monkeypatch.setattr(
        review_batches, "load_criteria", lambda: SimpleNamespace(reason_tags_approved=True)
    )


@pytest.fixture
def scripted(monkeypatch):
    monkeypatch.setattr(review_batches, "_rng", _ScriptedRng())


# --- builders -------------------------------------------------------------------------------


async def _epoch(db_factory, status: str = "active") -> uuid.UUID:
    async with db_factory() as db:
        epoch = LandscapeEpoch(status=status, snapshot=epochs.build_snapshot())
        db.add(epoch)
        await db.commit()
        return epoch.id


async def _ideas(
    db_factory, epoch_id: uuid.UUID, n: int, *, k: int, deep: str = "done"
) -> list[_Idea]:
    """``n`` ideas ranked in the given order in one complete cut with top ``k``, each with one
    S6 record of status ``deep``."""
    async with db_factory() as db:
        made = []
        for rank in range(1, n + 1):
            idea = LandscapeIdea(
                id=uuid.uuid4(), epoch_id=epoch_id,
                idea_key=f"biology;invented-rb-{next(_counter)}", idea_type="biology",
                direction="inhibit", targets=[{"hgnc_id": "HGNC:90001", "symbol": "INVRB"}],
                disease={"mondo_id": "MONDO:9999999", "label": "invented fibrosis"},
                version_hash="v1", in_current_cut=rank <= k,
            )
            db.add(idea)
            made.append(idea)
        await db.flush()
        for idea in made:
            db.add(LandscapeDeep(
                idea_id=idea.id, idea_version="v1", epoch_id=epoch_id, status=deep,
            ))
        db.add(LandscapeCut(
            epoch_id=epoch_id, status="complete", n=n, k=k,
            ordered_idea_ids=[str(idea.id) for idea in made], completed_at=func.now(),
        ))
        await db.commit()
        return [_Idea(idea.id, idea.idea_key) for idea in made]


async def _cards(
    db_factory, epoch_id: uuid.UUID, ideas: list[_Idea], *, assessed: bool = True
) -> list[_Card]:
    """One single-idea opportunity per idea, in order, each with a ``done`` assessment unless
    ``assessed`` is false."""
    async with db_factory() as db:
        opps = [LandscapeOpportunity(id=uuid.uuid4(), version_counter=1) for _ in ideas]
        db.add_all(opps)
        await db.flush()
        db.add_all(
            LandscapeOpportunityVersion(
                opportunity_id=opp.id, version=1, epoch_id=epoch_id,
                member_idea_keys=[idea.key], label="Invented programme", status="done",
            )
            for opp, idea in zip(opps, ideas, strict=True)
        )
        await db.flush()
        assessments = [
            LandscapeAssessment(
                id=uuid.uuid4(), epoch_id=epoch_id, opportunity_id=opp.id,
                opportunity_version=1, status="done", headline="An invented headline",
            )
            if assessed else None
            for opp in opps
        ]
        db.add_all(a for a in assessments if a is not None)
        await db.commit()
        return [
            _Card(opp.id, a.id if a is not None else None)
            for opp, a in zip(opps, assessments, strict=True)
        ]


async def _assess(
    db_factory, epoch_id: uuid.UUID, opportunity_id: uuid.UUID, *, status: str = "done",
    version: int = 1,
) -> uuid.UUID:
    async with db_factory() as db:
        row = LandscapeAssessment(
            id=uuid.uuid4(), epoch_id=epoch_id, opportunity_id=opportunity_id,
            opportunity_version=version, status=status,
        )
        db.add(row)
        await db.commit()
        return row.id


async def _batch(db_factory, issued_on: date, *, blind: bool = False) -> uuid.UUID:
    """A past batch written directly, with no cards; it counts toward the blind cadence."""
    async with db_factory() as db:
        batch = LandscapeReviewBatch(id=uuid.uuid4(), issued_on=issued_on, blind=blind)
        db.add(batch)
        await db.commit()
        return batch.id


async def _past_batches(db_factory, n: int) -> None:
    for w in range(1, n + 1):
        await _batch(db_factory, week(w))


async def _get(db_factory, model, row_id):
    async with db_factory() as db:
        return await db.get(model, row_id, populate_existing=True)


async def _items(db_factory, batch_id: uuid.UUID) -> list[LandscapeReviewBatchItem]:
    async with db_factory() as db:
        return list((
            await db.scalars(
                select(LandscapeReviewBatchItem)
                .where(LandscapeReviewBatchItem.batch_id == batch_id)
            )
        ).all())


async def _count(db_factory, model) -> int:
    async with db_factory() as db:
        return await db.scalar(select(func.count()).select_from(model))


async def _pairs(db_factory, rank: dict[uuid.UUID, int]) -> list[tuple]:
    """Every ``(batch, item)`` pair, a normal batch's items in ``rank`` order (card order)."""
    async with db_factory() as db:
        rows = (
            await db.execute(
                select(LandscapeReviewBatch, LandscapeReviewBatchItem)
                .join(
                    LandscapeReviewBatchItem,
                    LandscapeReviewBatchItem.batch_id == LandscapeReviewBatch.id,
                )
            )
        ).all()
    return sorted(
        ((batch, item) for batch, item in rows),
        key=lambda p: (p[0].issued_on, rank.get(p[1].opportunity_id, 0)),
    )


async def _decisions(db_factory) -> list[LandscapeDecision]:
    async with db_factory() as db:
        return list((await db.scalars(select(LandscapeDecision))).all())


async def _decide(db_factory, user, card: _Card, assessment_id: uuid.UUID | None = None) -> None:
    async with db_factory() as db:
        db.add(LandscapeDecision(
            opportunity_id=card.opp, reviewer_id=user.id, reviewer_name=user.name,
            decision="pursue", landscape_assessment_id=assessment_id or card.assessment,
        ))
        await db.commit()


async def _users(db_factory, *names: str) -> list:
    async with db_factory() as db:
        users = [await make_user(db, name=name) for name in names]
        await db.commit()
        return users


async def _probe_keys(db_factory, opportunity_ids) -> list[str]:
    async with db_factory() as db:
        keys = dict((
            await db.execute(
                select(
                    LandscapeOpportunityVersion.opportunity_id,
                    LandscapeOpportunityVersion.member_idea_keys,
                ).where(LandscapeOpportunityVersion.opportunity_id.in_(list(opportunity_ids)))
            )
        ).all())
    return [keys[o][0] for o in opportunity_ids]


async def _retired(db_factory, opportunity_id: uuid.UUID) -> bool:
    return (await _get(db_factory, LandscapeOpportunity, opportunity_id)).retired_at is not None


async def _retire(db_factory, opportunity_ids) -> None:
    async with db_factory() as db:
        await db.execute(
            update(LandscapeOpportunity)
            .where(LandscapeOpportunity.id.in_(list(opportunity_ids)))
            .values(retired_at=func.now())
        )
        await db.commit()


async def _set_deep(db_factory, idea_id: uuid.UUID, status: str) -> None:
    async with db_factory() as db:
        await db.execute(
            update(LandscapeDeep).where(LandscapeDeep.idea_id == idea_id).values(status=status)
        )
        await db.commit()


async def _set_assessment(db_factory, opportunity_id: uuid.UUID, status: str) -> None:
    async with db_factory() as db:
        await db.execute(
            update(LandscapeAssessment)
            .where(LandscapeAssessment.opportunity_id == opportunity_id)
            .values(status=status)
        )
        await db.commit()


async def _set_experts(db_factory, values: list[str]) -> None:
    async with db_factory() as db:
        await db.merge(AppSetting(key=review_batches.EXPERTS_KEY, value=json.dumps(values)))
        await db.commit()


def _rank(cards: list[_Card]) -> dict[uuid.UUID, int]:
    return {card.opp: n for n, card in enumerate(cards)}


# --- weekly batches -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ten_cards_same_batch_for_all(db_factory):
    epoch = await _epoch(db_factory)
    ideas = await _ideas(db_factory, epoch, 12, k=12)
    cards = list(reversed(await _cards(db_factory, epoch, list(reversed(ideas)))))
    async with db_factory() as db:
        # S7 places the twelfth idea first, ahead of S5's first (D30).
        db.add(LandscapeRanking(epoch_id=epoch, idea_id=ideas[11].id, position=1))
        await db.commit()
    ash, birch = await _users(db_factory, "Invented Reviewer Ash", "Invented Reviewer Birch")

    batch_id = await review_batches.issue_weekly_batch(db_factory, today=week(1))

    batch = await _get(db_factory, LandscapeReviewBatch, batch_id)
    assert batch.blind is False and batch.issued_on == week(1)
    items = await _items(db_factory, batch_id)
    assert {i.opportunity_id for i in items} == {c.opp for c in cards[:9]} | {cards[11].opp}
    assert all(i.position is None and not i.is_probe for i in items)
    # One batch for everyone: issuing again the same week adds nothing.
    assert await review_batches.issue_weekly_batch(db_factory, today=week(1)) is None
    assert await _count(db_factory, LandscapeReviewBatch) == 1
    pairs = await _pairs(db_factory, _rank(cards))
    shown = {c.opp: c.assessment for c in cards}
    for user in (ash, birch):
        assert review_batches.viewer_cards(pairs, [], shown, frozenset(), user) == []


@pytest.mark.asyncio
async def test_carry_over_is_per_viewer_and_not_counted(db_factory):
    epoch = await _epoch(db_factory)
    cards = await _cards(db_factory, epoch, await _ideas(db_factory, epoch, 25, k=25))
    ash, birch = await _users(db_factory, "Invented Reviewer Ash", "Invented Reviewer Birch")
    await review_batches.issue_weekly_batch(db_factory, today=week(1))
    for card in cards[:4]:
        await _decide(db_factory, ash, card)

    second = await review_batches.issue_weekly_batch(db_factory, today=week(2))

    assert {i.opportunity_id for i in await _items(db_factory, second)} == {
        c.opp for c in cards[10:20]
    }
    pairs = await _pairs(db_factory, _rank(cards))
    decisions = await _decisions(db_factory)
    shown = {c.opp: c.assessment for c in cards}
    ash_view = review_batches.viewer_cards(pairs, decisions, shown, frozenset(), ash)
    birch_view = review_batches.viewer_cards(pairs, decisions, shown, frozenset(), birch)
    assert [c["opportunity_id"] for c in ash_view] == [c.opp for c in cards[4:10]]
    assert [c["opportunity_id"] for c in birch_view] == [c.opp for c in cards[:10]]
    assert {c["kind"] for c in ash_view + birch_view} == {"carry_over"}


@pytest.mark.asyncio
async def test_fewer_than_ten_available(db_factory):
    epoch = await _epoch(db_factory)
    ideas = await _ideas(db_factory, epoch, 4, k=4)
    cards = await _cards(db_factory, epoch, ideas[:3])
    await _cards(db_factory, epoch, ideas[3:], assessed=False)

    batch_id = await review_batches.issue_weekly_batch(db_factory, today=week(1))

    assert {i.opportunity_id for i in await _items(db_factory, batch_id)} == {
        c.opp for c in cards
    }
    assert await review_batches.issue_weekly_batch(db_factory, today=week(2)) is None
    assert await _count(db_factory, LandscapeReviewBatch) == 1


@pytest.mark.asyncio
async def test_changed_card_shown_not_counted(db_factory):
    epoch = await _epoch(db_factory)
    cards = await _cards(db_factory, epoch, await _ideas(db_factory, epoch, 13, k=13))
    (ash,) = await _users(db_factory, "Invented Reviewer Ash")
    await review_batches.issue_weekly_batch(db_factory, today=week(1))
    await _decide(db_factory, ash, cards[0])
    await _decide(db_factory, ash, cards[1])
    second = await review_batches.issue_weekly_batch(db_factory, today=week(2))
    newer = await _assess(db_factory, epoch, cards[0].opp, version=2)

    shown = {c.opp: c.assessment for c in cards} | {cards[0].opp: newer}
    view = review_batches.viewer_cards(
        await _pairs(db_factory, _rank(cards)), await _decisions(db_factory), shown,
        frozenset(), ash,
    )

    changed = [c for c in view if c["kind"] == "changed"]
    assert [(c["opportunity_id"], c["assessment_id"]) for c in changed] == [(cards[0].opp, newer)]
    assert [c["opportunity_id"] for c in view if c["kind"] == "carry_over"] == [
        c.opp for c in cards[2:10]
    ]
    # The next batch took the three never-issued cards; the changed card took no place.
    assert {i.opportunity_id for i in await _items(db_factory, second)} == {
        c.opp for c in cards[10:13]
    }


# --- blind batches and probes ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_fourth_blind_with_two_probes_shuffled(db_factory, scripted):
    epoch = await _epoch(db_factory)
    ideas = await _ideas(db_factory, epoch, 30, k=10)
    cards = await _cards(db_factory, epoch, ideas[:10])
    await _past_batches(db_factory, 3)
    probes = await review_batches.draw_probes(db_factory, for_issue_on=week(4))
    assert len(probes) == 2
    for probe in probes:
        await _assess(db_factory, epoch, probe)

    batch_id = await review_batches.issue_weekly_batch(db_factory, today=week(4))

    assert (await _get(db_factory, LandscapeReviewBatch, batch_id)).blind is True
    items = sorted(await _items(db_factory, batch_id), key=lambda i: i.position)
    assert [i.position for i in items] == list(range(1, 11))
    # The scripted shuffle reverses the selection: the eight normal cards in card order, then
    # the two probes.
    assert {i.opportunity_id for i in items[:2]} == set(probes)
    assert [i.opportunity_id for i in items[2:]] == [c.opp for c in reversed(cards[:8])]
    assert {i.opportunity_id for i in items if i.is_probe} == set(probes)


@pytest.mark.asyncio
async def test_blind_draw_from_k_to_5k(db_factory):
    epoch = await _epoch(db_factory)
    ideas = await _ideas(db_factory, epoch, 15, k=2)
    await _past_batches(db_factory, 3)

    drawn: list[str] = []
    for _ in range(4):
        probes = await review_batches.draw_probes(db_factory, for_issue_on=week(4))
        assert len(probes) == 2
        drawn += await _probe_keys(db_factory, probes)
        await _retire(db_factory, probes)

    # Ranks K+1..5K are ideas 3..10; the top K and ranks past 5K are never drawn, and no idea
    # is drawn twice.
    assert sorted(drawn) == sorted(i.key for i in ideas[2:10])
    assert await review_batches.draw_probes(db_factory, for_issue_on=week(4)) == []


@pytest.mark.asyncio
async def test_probes_drawn_one_cycle_ahead(db_factory):
    epoch = await _epoch(db_factory)
    await _ideas(db_factory, epoch, 20, k=2)
    await _past_batches(db_factory, 2)

    assert await review_batches.draw_probes(db_factory, for_issue_on=week(3)) == []
    await _batch(db_factory, week(3))
    first = await review_batches.draw_probes(db_factory, for_issue_on=week(4))
    again = await review_batches.draw_probes(db_factory, for_issue_on=week(4))

    assert len(first) == 2 and sorted(again) == sorted(first)
    async with db_factory() as db:
        count = await db.scalar(
            select(func.count()).select_from(LandscapeOpportunityVersion)
            .where(LandscapeOpportunityVersion.is_probe.is_(True))
        )
    assert count == 2


@pytest.mark.asyncio
async def test_probe_redrawn_on_gate_top_k_or_retired_epoch(db_factory, scripted):
    old = await _epoch(db_factory)
    ideas = await _ideas(db_factory, old, 20, k=2)
    await _past_batches(db_factory, 3)
    p1, p2 = await review_batches.draw_probes(db_factory, for_issue_on=week(4))
    assert await _probe_keys(db_factory, [p1, p2]) == [ideas[2].key, ideas[3].key]

    await _set_deep(db_factory, ideas[2].id, "excluded:deep_gate")
    p2_kept, p3 = await review_batches.draw_probes(db_factory, for_issue_on=week(4))
    assert p2_kept == p2 and await _retired(db_factory, p1)
    assert await _probe_keys(db_factory, [p3]) == [ideas[4].key]

    async with db_factory() as db:
        await db.execute(
            update(LandscapeIdea).where(LandscapeIdea.id == ideas[3].id)
            .values(in_current_cut=True)
        )
        await db.commit()
    p3_kept, p4 = await review_batches.draw_probes(db_factory, for_issue_on=week(4))
    assert p3_kept == p3 and await _retired(db_factory, p2)
    assert await _probe_keys(db_factory, [p4]) == [ideas[5].key]

    async with db_factory() as db:
        await db.execute(
            update(LandscapeEpoch).where(LandscapeEpoch.id == old).values(status="retired")
        )
        await db.commit()
    new = await _epoch(db_factory)
    fresh = await _ideas(db_factory, new, 20, k=2)
    redrawn = await review_batches.draw_probes(db_factory, for_issue_on=week(4))
    assert await _retired(db_factory, p3) and await _retired(db_factory, p4)
    assert await _probe_keys(db_factory, redrawn) == [fresh[2].key, fresh[3].key]


@pytest.mark.asyncio
async def test_issued_probe_with_failed_s9_stays(db_factory, scripted):
    epoch = await _epoch(db_factory)
    ideas = await _ideas(db_factory, epoch, 30, k=10)
    cards = await _cards(db_factory, epoch, ideas[:10])
    await _past_batches(db_factory, 3)
    probes = await review_batches.draw_probes(db_factory, for_issue_on=week(4))
    for probe in probes:
        await _assess(db_factory, epoch, probe)
    await review_batches.issue_weekly_batch(db_factory, today=week(4))
    failed = probes[0]
    await _set_assessment(db_factory, failed, "failed:invalid")
    await _set_deep(db_factory, ideas[10].id, "refused")
    for w in (5, 6, 7):
        await _batch(db_factory, week(w))

    redrawn = await review_batches.draw_probes(db_factory, for_issue_on=week(8))

    assert len(redrawn) == 2 and failed not in redrawn
    assert not await _retired(db_factory, failed)
    shown = {c.opp: c.assessment for c in cards} | {failed: None}
    # This week's batch is the newest; the blind batch of week 4 is an earlier one.
    pairs = await _pairs(db_factory, _rank(cards)) + [_pair(week(8), uuid.uuid4())]
    view = review_batches.viewer_cards(pairs, [], shown, frozenset(), SimpleNamespace(id=None))
    (card,) = [c for c in view if c["opportunity_id"] == failed]
    assert card["kind"] == "carry_over" and card["no_assessment"] is True


@pytest.mark.asyncio
async def test_probe_not_ready_makes_a_normal_batch_and_the_next_blind(db_factory):
    epoch = await _epoch(db_factory)
    ideas = await _ideas(db_factory, epoch, 40, k=12)
    await _cards(db_factory, epoch, ideas[:12])
    await _past_batches(db_factory, 3)
    probes = await review_batches.draw_probes(db_factory, for_issue_on=week(4))
    await _assess(db_factory, epoch, probes[0])

    normal = await review_batches.issue_weekly_batch(db_factory, today=week(4))

    assert (await _get(db_factory, LandscapeReviewBatch, normal)).blind is False
    items = await _items(db_factory, normal)
    assert len(items) == 10
    assert all(not i.is_probe and i.position is None for i in items)
    assert not {i.opportunity_id for i in items} & set(probes)

    await _assess(db_factory, epoch, probes[1])
    blind = await review_batches.issue_weekly_batch(db_factory, today=week(5))

    assert (await _get(db_factory, LandscapeReviewBatch, blind)).blind is True
    items = await _items(db_factory, blind)
    assert {i.opportunity_id for i in items if i.is_probe} == set(probes)
    assert sorted(i.position for i in items) == list(range(1, len(items) + 1))


@pytest.mark.asyncio
async def test_replaced_probe_retired(db_factory, scripted):
    epoch = await _epoch(db_factory)
    ideas = await _ideas(db_factory, epoch, 30, k=8)
    await _cards(db_factory, epoch, ideas[:8])
    await _past_batches(db_factory, 3)
    p1, p2 = await review_batches.draw_probes(db_factory, for_issue_on=week(4))
    await _assess(db_factory, epoch, p1, status="refused")
    await _assess(db_factory, epoch, p2)

    kept, p3 = await review_batches.draw_probes(db_factory, for_issue_on=week(4))
    await _assess(db_factory, epoch, p3)
    batch_id = await review_batches.issue_weekly_batch(db_factory, today=week(4))

    assert kept == p2 and await _retired(db_factory, p1)
    items = await _items(db_factory, batch_id)
    assert {i.opportunity_id for i in items if i.is_probe} == {p2, p3}
    assert p1 not in {i.opportunity_id for i in items}


@pytest.mark.asyncio
async def test_probe_redrawn_on_terminal_failure(db_factory, scripted):
    epoch = await _epoch(db_factory)
    ideas = await _ideas(db_factory, epoch, 20, k=2)
    await _past_batches(db_factory, 3)
    p1, p2 = await review_batches.draw_probes(db_factory, for_issue_on=week(4))
    await _set_deep(db_factory, ideas[2].id, "failed:timeout")
    await _set_deep(db_factory, ideas[3].id, "refused")

    redrawn = await review_batches.draw_probes(db_factory, for_issue_on=week(4))

    assert await _retired(db_factory, p1) and await _retired(db_factory, p2)
    assert await _probe_keys(db_factory, redrawn) == [ideas[4].key, ideas[5].key]


# --- carry-overs ----------------------------------------------------------------------------


def _pair(issued_on: date, opportunity_id, *, blind: bool = False, position=None, batch=None):
    batch = batch or SimpleNamespace(id=uuid.uuid4(), issued_on=issued_on, blind=blind)
    return batch, SimpleNamespace(opportunity_id=opportunity_id, position=position)


def test_carry_over_grouped_by_batch_keeping_blind_order():
    a, b, c, d, e, f, g = (uuid.uuid4() for _ in range(7))
    first = SimpleNamespace(id=uuid.uuid4(), issued_on=week(1), blind=False)
    blind = SimpleNamespace(id=uuid.uuid4(), issued_on=week(2), blind=True)
    third = SimpleNamespace(id=uuid.uuid4(), issued_on=week(3), blind=False)
    pairs = [
        _pair(week(1), a, batch=first), _pair(week(1), b, batch=first),
        _pair(week(2), c, batch=blind, position=2), _pair(week(2), d, batch=blind, position=1),
        _pair(week(3), e, batch=third), _pair(week(3), f, batch=third),
        _pair(week(4), g),
    ]
    shown = {o: uuid.uuid4() for o in (a, b, c, d, e, f, g)}

    view = review_batches.viewer_cards(pairs, [], shown, frozenset(), SimpleNamespace(id=1))

    assert [card["opportunity_id"] for card in view] == [e, f, d, c, a, b]
    assert [card["blind"] for card in view] == [False, False, True, True, False, False]


def test_carry_over_drops_only_retired_cards():
    retired_undecided, unassessed, retired_changed, changed, newest = (
        uuid.uuid4() for _ in range(5)
    )
    earlier = SimpleNamespace(id=uuid.uuid4(), issued_on=week(1), blind=False)
    pairs = [
        _pair(week(1), o, batch=earlier)
        for o in (retired_undecided, unassessed, retired_changed, changed)
    ] + [_pair(week(2), newest)]
    viewer = SimpleNamespace(id=uuid.uuid4())
    decisions = [
        SimpleNamespace(opportunity_id=o, reviewer_id=viewer.id, landscape_assessment_id=uuid.uuid4())
        for o in (retired_changed, changed)
    ]
    shown = {retired_undecided: uuid.uuid4(), unassessed: None, retired_changed: uuid.uuid4(),
             changed: uuid.uuid4(), newest: uuid.uuid4()}

    view = review_batches.viewer_cards(
        pairs, decisions, shown, frozenset({retired_undecided, retired_changed}), viewer
    )

    assert [(c["opportunity_id"], c["kind"], c["no_assessment"]) for c in view] == [
        (unassessed, "carry_over", True), (changed, "changed", False),
    ]


# --- refusal and expert sample --------------------------------------------------------------


@pytest.mark.asyncio
async def test_refuses_until_reason_tags_approved(db_factory, monkeypatch):
    epoch = await _epoch(db_factory)
    await _cards(db_factory, epoch, await _ideas(db_factory, epoch, 3, k=3))
    monkeypatch.setattr(
        review_batches, "load_criteria", lambda: SimpleNamespace(reason_tags_approved=False)
    )

    assert await review_batches.issue_weekly_batch(db_factory, today=week(1)) is None
    assert await _count(db_factory, LandscapeReviewBatch) == 0

    monkeypatch.setattr(
        review_batches, "load_criteria", lambda: SimpleNamespace(reason_tags_approved=True)
    )
    assert await review_batches.issue_weekly_batch(db_factory, today=week(1)) is not None


@pytest.mark.asyncio
async def test_expert_sample_skips_decided_or_reviewed_items(db_factory):
    epoch = await _epoch(db_factory)
    cards = await _cards(db_factory, epoch, await _ideas(db_factory, epoch, 12, k=12))
    await review_batches.issue_weekly_batch(db_factory, today=week(1))
    (expert,) = await _users(db_factory, "Invented Expert Cedar")
    await _decide(db_factory, expert, cards[0])
    async with db_factory() as db:
        db.add(LandscapeReview(
            opportunity_id=cards[1].opp, reviewer_id=expert.id, reviewer_name=expert.name,
            score=3, landscape_assessment_id=cards[1].assessment,
        ))
        await db.commit()
    await _set_experts(db_factory, [str(expert.id)])

    made = await review_batches.expert_sample(db_factory, quarter="2026-Q4")

    async with db_factory() as db:
        rows = (await db.scalars(select(LandscapeExpertJudgment))).all()
    assert sorted(r.id for r in rows) == sorted(made)
    by_opp = {r.opportunity_id: r for r in rows}
    assert set(by_opp) == {c.opp for c in cards[2:]}
    for card in cards[2:]:
        row = by_opp[card.opp]
        assert row.landscape_assessment_id == card.assessment
        assert row.issued_before_draw is (card in cards[:10])
        assert row.submitted_at is None and row.expert_id == expert.id


@pytest.mark.asyncio
async def test_expert_sample_ten_per_expert(db_factory):
    epoch = await _epoch(db_factory)
    ideas = await _ideas(db_factory, epoch, 20, k=17)
    await _cards(db_factory, epoch, ideas[:15])
    (unassessed,) = await _cards(db_factory, epoch, ideas[15:16], assessed=False)
    pending_probe = await make_probe(db_factory, epoch_id=epoch, idea_id=ideas[18].id)
    await _assess(db_factory, epoch, pending_probe)
    cedar, dogwood = await _users(db_factory, "Invented Expert Cedar", "Invented Expert Dogwood")
    await _set_experts(
        db_factory, [str(cedar.id), "not-a-user-id", str(uuid.uuid4()), str(dogwood.id)]
    )

    made = await review_batches.expert_sample(db_factory, quarter="2026-Q4")

    assert len(made) == 20
    async with db_factory() as db:
        rows = (await db.scalars(select(LandscapeExpertJudgment))).all()
    for expert in (cedar, dogwood):
        mine = [r.opportunity_id for r in rows if r.expert_id == expert.id]
        assert len(mine) == 10 and len(set(mine)) == 10
    sampled = {r.opportunity_id for r in rows}
    assert unassessed.opp not in sampled and pending_probe not in sampled
    assert await review_batches.expert_sample(db_factory, quarter="2026-Q4") == []
