"""Weekly review batches, blind-batch probes and the quarterly expert sample (spec §13.1,
§10.6, §7.9; L45-L47, L52-L55, D30, D48, D50, D62, D63).

**Weekly batch** (D48). One batch a week, the same for everyone: up to ``BATCH_SIZE``
opportunities never issued before, in card order (D30, ``rerank.card_order``). An opportunity
is available once it has a ``done`` S9 assessment of its latest version in the active epoch,
so every issued card has something to show. A normal batch leaves ``position`` NULL, so its
cards follow the live card order; a blind batch stores its shuffled order in ``position``.

**Blind cadence** (L54, D62). Every fourth batch is blind: a batch is due blind once three
batches have been issued since the last blind one (or since the first). A blind batch holds
``BATCH_SIZE - PROBES_PER_BLIND`` of the normal selection plus the two probes ``draw_probes``
made a cycle earlier. When either probe is not ready at issue (no ``done`` S9 in the active
epoch), the batch is issued as a normal one; the cadence count then still says "due", so the
next batch is blind instead.

**Probes** (spec §7.9, D62). A drawn probe is a single-idea probe opportunity
(``opportunities.make_probe``) that is in no review batch yet. Until issue it is replaced by a
fresh draw from ranks K+1…5K of the active cut, and retired, when its S6 is
``excluded:deep_gate``, its S6 or S9 ends ``refused`` or ``failed:*``, its idea enters the top
K, or the epoch it was drawn in is retired. An issued probe is never redrawn or retired.

**Carry-overs** (L47, D62) are per viewer and computed by the pure ``viewer_cards``.

**Expert sample** (L55, D63). ``app_settings.landscape_experts`` holds a JSON list of user ids.
"""
from __future__ import annotations

import json
import logging
import random
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta
from typing import Any

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.landscaping import epochs
from src.landscaping.criteria import load_criteria
from src.landscaping.stages.opportunities import make_probe
from src.landscaping.stages.rerank import card_order
from src.models import AppSetting, User
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

logger = logging.getLogger(__name__)

#: Cards in a weekly batch, probes included (D48).
BATCH_SIZE = 10
#: Every this-many-th batch is blind (L54).
BLIND_EVERY = 4
#: Probes in a blind batch (spec §10.6).
PROBES_PER_BLIND = 2
#: Probes are drawn from ranks K+1 to this many times K (L52).
PROBE_RANK_SPAN = 5
#: Items drawn per expert per quarter (L55).
EXPERT_SAMPLE_SIZE = 10
#: The shortest gap between two batch issues.
ISSUE_INTERVAL = timedelta(days=7)
#: ``app_settings`` key: a JSON list of expert user ids (spec §10.6, §12.3).
EXPERTS_KEY = "landscape_experts"
#: The S6 status of an idea whose gate is not met (spec §7.7).
_DEEP_GATE = "excluded:deep_gate"

#: Draws and shuffles; tests replace it with a seeded or scripted one.
_rng = random.Random()


# --- shared queries ---------------------------------------------------------------------------


def _terminal(status: str | None) -> bool:
    return status is not None and (status == "refused" or status.startswith("failed:"))


async def _current_cut(db: AsyncSession, epoch_id: uuid.UUID) -> LandscapeCut | None:
    """The epoch's newest complete cut (a ``boundary_pending`` one is not current yet)."""
    return await db.scalar(
        select(LandscapeCut)
        .where(LandscapeCut.epoch_id == epoch_id, LandscapeCut.status == "complete")
        .order_by(LandscapeCut.completed_at.desc().nulls_last(), LandscapeCut.created_at.desc())
        .limit(1)
    )


async def _latest_versions(
    db: AsyncSession, *, probe: bool
) -> list[LandscapeOpportunityVersion]:
    """The latest version of every live opportunity of the given kind, oldest first."""
    V, Opp = LandscapeOpportunityVersion, LandscapeOpportunity
    return list(
        (
            await db.scalars(
                select(V).join(Opp, Opp.id == V.opportunity_id)
                .where(
                    V.version == Opp.version_counter, Opp.retired_at.is_(None),
                    V.is_probe.is_(probe),
                )
                .order_by(Opp.created_at, Opp.id)
            )
        ).all()
    )


async def _issued_ids(db: AsyncSession) -> set[uuid.UUID]:
    """Every opportunity that has been in a weekly batch."""
    return set((await db.scalars(select(LandscapeReviewBatchItem.opportunity_id))).all())


async def _done_assessments(
    db: AsyncSession, epoch_id: uuid.UUID
) -> dict[tuple[uuid.UUID, int], uuid.UUID]:
    """``(opportunity_id, version) -> assessment id`` of the epoch's ``done`` S9 rows."""
    A = LandscapeAssessment
    rows = (
        await db.execute(
            select(A.opportunity_id, A.opportunity_version, A.id).where(
                A.epoch_id == epoch_id, A.status == "done", A.opportunity_id.is_not(None)
            )
        )
    ).all()
    return {(opp, version): row_id for opp, version, row_id in rows}


async def _blind_due(db: AsyncSession, issue_on: date) -> bool:
    """Whether the batch issued on ``issue_on`` is due blind: three batches issued before it
    since the last blind one (L54). A deferred blind batch (D62) stays due."""
    rows = (
        await db.execute(
            select(LandscapeReviewBatch.blind)
            .where(LandscapeReviewBatch.issued_on < issue_on)
            .order_by(LandscapeReviewBatch.issued_on.desc())
        )
    ).scalars().all()
    since = 0
    for blind in rows:
        if blind:
            break
        since += 1
    return since >= BLIND_EVERY - 1


# --- probes -----------------------------------------------------------------------------------


async def _pending_probes(db: AsyncSession) -> list[LandscapeOpportunityVersion]:
    """Drawn probes not yet issued, oldest first."""
    issued = await _issued_ids(db)
    return [v for v in await _latest_versions(db, probe=True) if v.opportunity_id not in issued]


async def _newest_deep_status(
    db: AsyncSession, epoch_id: uuid.UUID, idea_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Each idea's newest S6 status in the epoch."""
    ids = list(idea_ids)
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(LandscapeDeep.idea_id, LandscapeDeep.status)
            .where(LandscapeDeep.epoch_id == epoch_id, LandscapeDeep.idea_id.in_(ids))
            .order_by(LandscapeDeep.created_at, LandscapeDeep.id)
        )
    ).all()
    return {idea_id: status for idea_id, status in rows}


def _idea_failure(idea: LandscapeIdea | None, deep_status: str | None) -> str | None:
    """Why an idea cannot be (or stay) a probe, by its own facts (D62); None when it can."""
    if idea is None:
        return "its idea is gone from its epoch"
    if idea.in_current_cut:
        return "its idea entered the top K"
    if deep_status == _DEEP_GATE:
        return "its S6 found a gate not met"
    if _terminal(deep_status):
        return f"its S6 ended {deep_status}"
    return None


async def _probe_failure(db: AsyncSession, version: LandscapeOpportunityVersion) -> str | None:
    """Why a drawn, unissued probe must be replaced (D62); None while it may stay."""
    epoch_status = await db.scalar(
        select(LandscapeEpoch.status).where(LandscapeEpoch.id == version.epoch_id)
    )
    if epoch_status in (None, "retired"):
        return "the epoch it was drawn in is retired"
    keys = list(version.member_idea_keys or [])
    idea = None
    if keys:
        idea = await db.scalar(
            select(LandscapeIdea).where(
                LandscapeIdea.epoch_id == version.epoch_id, LandscapeIdea.idea_key == keys[0]
            )
        )
    deep = None
    if idea is not None:
        deep = (await _newest_deep_status(db, version.epoch_id, [idea.id])).get(idea.id)
    failure = _idea_failure(idea, deep)
    if failure is not None:
        return failure
    A = LandscapeAssessment
    s9 = await db.scalar(
        select(A.status)
        .where(
            A.epoch_id == version.epoch_id, A.opportunity_id == version.opportunity_id,
            A.opportunity_version == version.version,
        )
        .order_by(A.created_at.desc(), A.id.desc())
        .limit(1)
    )
    return f"its S9 ended {s9}" if _terminal(s9) else None


async def _keep_pending(db: AsyncSession) -> list[uuid.UUID]:
    """Retire every drawn probe that must be replaced (D62); the others' ids, oldest first.
    Does not commit."""
    kept: list[uuid.UUID] = []
    for version in await _pending_probes(db):
        reason = await _probe_failure(db, version)
        if reason is None:
            kept.append(version.opportunity_id)
            continue
        logger.info("probe %s replaced: %s", version.opportunity_id, reason)
        await db.execute(
            update(LandscapeOpportunity)
            .where(LandscapeOpportunity.id == version.opportunity_id)
            .values(retired_at=func.now(), updated_at=func.now())
        )
    return kept


async def _probe_keys(db: AsyncSession) -> set[str]:
    """Every idea key that has ever been a probe, so no idea is drawn twice."""
    rows = (
        await db.scalars(
            select(LandscapeOpportunityVersion.member_idea_keys)
            .where(LandscapeOpportunityVersion.is_probe.is_(True))
        )
    ).all()
    return {key for keys in rows for key in (keys or [])}


async def _drawable(db: AsyncSession, epoch_id: uuid.UUID) -> list[uuid.UUID]:
    """Ideas at ranks K+1…5K of the epoch's current cut that may become probes, in rank
    order (L52, D62)."""
    cut = await _current_cut(db, epoch_id)
    if cut is None or not cut.k:
        return []
    ranked = [uuid.UUID(str(i)) for i in (cut.ordered_idea_ids or [])[cut.k:PROBE_RANK_SPAN * cut.k]]
    if not ranked:
        return []
    ideas = {
        idea.id: idea
        for idea in (
            await db.scalars(
                select(LandscapeIdea)
                .where(LandscapeIdea.epoch_id == epoch_id, LandscapeIdea.id.in_(ranked))
            )
        ).all()
    }
    used = await _probe_keys(db)
    deep = await _newest_deep_status(db, epoch_id, ideas)
    return [
        idea_id for idea_id in ranked
        if idea_id in ideas and ideas[idea_id].idea_key not in used
        and _idea_failure(ideas[idea_id], deep.get(idea_id)) is None
    ]


async def draw_probes(db_factory, *, for_issue_on: date) -> list[uuid.UUID]:
    """The probe opportunities for the blind batch to be issued on ``for_issue_on`` (spec
    §10.6, D62); [] when that batch is not due blind or there is no active epoch.

    Called one batch cycle ahead, so S6 (full text included, L64) and S9 can run on the probes
    before issue; safe to call every loop. Each call first retires every drawn, unissued probe
    that must be replaced (see the module docstring), then draws fresh ideas at random from
    ranks K+1…5K of the active cut until there are ``PROBES_PER_BLIND``, never an idea that has
    been a probe before. Fewer come back when the ranks run out. Issued probes are not touched.
    """
    async with db_factory() as db:
        epoch = await epochs.active_epoch(db)
        if epoch is None or not await _blind_due(db, for_issue_on):
            return []
        epoch_id = epoch.id
        kept = await _keep_pending(db)
        need = PROBES_PER_BLIND - len(kept)
        drawable = await _drawable(db, epoch_id) if need > 0 else []
        await db.commit()
    chosen = _rng.sample(drawable, min(need, len(drawable))) if need > 0 else []
    made = [await make_probe(db_factory, epoch_id=epoch_id, idea_id=i) for i in chosen]
    return kept + made


async def _ready_probes(db: AsyncSession, epoch_id: uuid.UUID) -> list[uuid.UUID] | None:
    """The two oldest drawn probes when both are ready for issue (drawn in the active epoch,
    not due for replacement, S9 ``done``); None otherwise (D62)."""
    pending = (await _pending_probes(db))[:PROBES_PER_BLIND]
    if len(pending) < PROBES_PER_BLIND:
        return None
    done = await _done_assessments(db, epoch_id)
    for version in pending:
        if version.epoch_id != epoch_id or (version.opportunity_id, version.version) not in done:
            return None
        if await _probe_failure(db, version) is not None:
            return None
    return [version.opportunity_id for version in pending]


# --- weekly batch -----------------------------------------------------------------------------


async def _available(db: AsyncSession, epoch_id: uuid.UUID) -> list[uuid.UUID]:
    """Never-issued, non-probe opportunities with a ``done`` assessment of their latest version
    in the epoch, in card order (D30)."""
    issued = await _issued_ids(db)
    done = await _done_assessments(db, epoch_id)
    versions = [
        v for v in await _latest_versions(db, probe=False)
        if v.opportunity_id not in issued and (v.opportunity_id, v.version) in done
    ]
    if not versions:
        return []
    keys = {key for v in versions for key in (v.member_idea_keys or [])}
    idea_ids = dict(
        (
            await db.execute(
                select(LandscapeIdea.idea_key, LandscapeIdea.id)
                .where(LandscapeIdea.epoch_id == epoch_id, LandscapeIdea.idea_key.in_(keys))
            )
        ).all()
    )
    cards = {
        v.opportunity_id: [idea_ids[k] for k in (v.member_idea_keys or []) if k in idea_ids]
        for v in versions
    }
    s7 = dict(
        (
            await db.execute(
                select(LandscapeRanking.idea_id, LandscapeRanking.position)
                .where(LandscapeRanking.epoch_id == epoch_id)
            )
        ).all()
    )
    cut = await _current_cut(db, epoch_id)
    s5 = {
        uuid.UUID(str(idea_id)): n
        for n, idea_id in enumerate((cut.ordered_idea_ids or []) if cut else [], start=1)
    }
    return card_order(cards, s7_positions=s7, s5_ranks=s5)


async def issue_weekly_batch(db_factory, *, today: date) -> uuid.UUID | None:
    """Issue this week's batch, the same for everyone (spec §13.1, D48); its id, or None when
    nothing was issued.

    Nothing is issued while the criteria file's ``reason_tags_approved`` is false (it is read
    from disk: reason tags belong to no epoch, D61), when a batch was issued less than
    ``ISSUE_INTERVAL`` before ``today``, before the first active epoch, or when no opportunity
    is available (fewer than ``BATCH_SIZE`` make a smaller batch). A batch due blind takes
    the two ready probes and is shuffled into ``position``; with a probe not ready it is issued
    normal and the next batch is due blind instead (D62).
    """
    if not load_criteria().reason_tags_approved:
        logger.warning("weekly batch not issued: the criteria file's reason tags are not approved")
        return None
    async with db_factory() as db:
        last = await db.scalar(select(func.max(LandscapeReviewBatch.issued_on)))
        if last is not None and today - last < ISSUE_INTERVAL:
            return None
        epoch = await epochs.active_epoch(db)
        if epoch is None:
            return None
        available = await _available(db, epoch.id)
        probes = None
        if await _blind_due(db, today):
            probes = await _ready_probes(db, epoch.id)
            if probes is None:
                logger.info("blind batch deferred: a probe is not ready; issuing a normal batch")
        if probes:
            cards = available[:BATCH_SIZE - len(probes)] + probes
            _rng.shuffle(cards)
            rows = [(opp, opp in probes, n) for n, opp in enumerate(cards, start=1)]
        else:
            rows = [(opp, False, None) for opp in available[:BATCH_SIZE]]
        if not rows:
            return None
        batch = LandscapeReviewBatch(id=uuid.uuid4(), issued_on=today, blind=bool(probes))
        db.add(batch)
        await db.flush()
        db.add_all(
            LandscapeReviewBatchItem(
                batch_id=batch.id, opportunity_id=opp, is_probe=is_probe, position=position,
            )
            for opp, is_probe, position in rows
        )
        await db.commit()
        logger.info("issued %s batch %s: %d cards", "blind" if probes else "normal",
                    batch.id, len(rows))
        return batch.id


def viewer_cards(
    batch_items: Sequence[tuple[Any, Any]],
    decisions: Iterable[Any],
    shown: Mapping[uuid.UUID, uuid.UUID | None],
    retired: frozenset[uuid.UUID],
    user: Any,
) -> list[dict]:
    """The viewer's carry-overs and "changed since your decision" cards (spec §13.1; L47, D50,
    D62). Neither counts toward a batch's ``BATCH_SIZE``.

    - ``batch_items``: ``(batch, item)`` pairs (``LandscapeReviewBatch``,
      ``LandscapeReviewBatchItem`` or alike) of every issued batch, this week's included; it is
      the newest by ``issued_on`` and is left out, since the viewer sees it whole. Within a
      normal batch the pairs must come in live card order (D30, ``rerank.card_order``); a blind
      batch is put in its stored ``position`` order here.
    - ``decisions``: quick decisions (anyone's; only ``user``'s, by ``reviewer_id``, are read).
    - ``shown``: opportunity -> the assessment the page shows, None when none is completed in
      the active epoch.
    - ``retired``: retired opportunities; they are neither carried over nor shown as changed.

    Returns dicts ``{"opportunity_id", "batch_id", "issued_on", "blind", "kind",
    "assessment_id", "no_assessment"}``, ``kind`` being ``"carry_over"`` (no quick decision
    by ``user``) or ``"changed"`` (decided on an assessment other than the one shown). Carry-
    overs come first, then changed cards, each grouped by batch, newest batch first.
    """
    if not batch_items:
        return []
    newest = max(batch.issued_on for batch, _ in batch_items)
    decided = {d.opportunity_id: d.landscape_assessment_id for d in decisions
               if d.reviewer_id == user.id}
    groups: dict[Any, tuple[Any, list]] = {}
    for batch, item in batch_items:
        if batch.issued_on == newest or item.opportunity_id in retired:
            continue
        groups.setdefault(batch.id, (batch, []))[1].append(item)
    carry: list[dict] = []
    changed: list[dict] = []
    for batch, items in sorted(groups.values(), key=lambda g: g[0].issued_on, reverse=True):
        if batch.blind:
            items = sorted(items, key=lambda i: (i.position is None, i.position or 0))
        for item in items:
            opportunity_id = item.opportunity_id
            assessment_id = shown.get(opportunity_id)
            card = {
                "opportunity_id": opportunity_id, "batch_id": batch.id,
                "issued_on": batch.issued_on, "blind": bool(batch.blind),
                "assessment_id": assessment_id, "no_assessment": assessment_id is None,
            }
            if opportunity_id not in decided:
                carry.append({**card, "kind": "carry_over"})
            elif assessment_id is not None and assessment_id != decided[opportunity_id]:
                changed.append({**card, "kind": "changed"})
    return carry + changed


# --- expert sample ----------------------------------------------------------------------------


async def _experts(db: AsyncSession) -> list[User]:
    """The marked experts (``app_settings.landscape_experts``), in the stored order."""
    raw = await db.scalar(select(AppSetting.value).where(AppSetting.key == EXPERTS_KEY))
    try:
        values = json.loads(raw) if raw else []
    except ValueError:
        logger.warning("%s is not JSON; no expert sample drawn", EXPERTS_KEY)
        return []
    ids: list[uuid.UUID] = []
    for value in values if isinstance(values, list) else []:
        try:
            ids.append(uuid.UUID(str(value)))
        except ValueError:
            logger.warning("%s holds a value that is not a user id: %r", EXPERTS_KEY, value)
    users = {u.id: u for u in (await db.scalars(select(User).where(User.id.in_(ids)))).all()}
    return [users[i] for i in dict.fromkeys(ids) if i in users]


async def _sample_pool(db: AsyncSession, epoch_id: uuid.UUID) -> dict[uuid.UUID, uuid.UUID]:
    """Assessed and probe cards (L55): live opportunities with a ``done`` assessment of their
    latest version in the epoch, probes only once issued. Opportunity -> assessment id."""
    done = await _done_assessments(db, epoch_id)
    issued = await _issued_ids(db)
    pool: dict[uuid.UUID, uuid.UUID] = {}
    for probe in (False, True):
        for v in await _latest_versions(db, probe=probe):
            assessment_id = done.get((v.opportunity_id, v.version))
            if assessment_id is not None and (not probe or v.opportunity_id in issued):
                pool[v.opportunity_id] = assessment_id
    return pool


async def _engaged(db: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    """Opportunities the user has a quick decision or a review on (D63)."""
    decided = (
        await db.scalars(
            select(LandscapeDecision.opportunity_id).where(LandscapeDecision.reviewer_id == user_id)
        )
    ).all()
    reviewed = (
        await db.scalars(
            select(LandscapeReview.opportunity_id).where(LandscapeReview.reviewer_id == user_id)
        )
    ).all()
    return set(decided) | set(reviewed)


async def expert_sample(db_factory, *, quarter: str) -> list[uuid.UUID]:
    """Draw the quarter's expert sample (spec §10.6; L55, D63): for each marked expert,
    ``EXPERT_SAMPLE_SIZE`` items at random (fewer when the pool runs short) from the assessed
    and probe cards on which they have no quick decision or review. ``quarter`` is ``YYYY-Qn``.

    Writes one open ``LandscapeExpertJudgment`` per item, holding the assessment it will be
    compared with and whether the item was in a batch issued before the draw; returns their
    ids. An expert who already has items for ``quarter`` is skipped, so a repeated call draws
    nothing new.
    """
    async with db_factory() as db:
        experts = await _experts(db)
        epoch = await epochs.active_epoch(db)
        if epoch is None or not experts:
            return []
        pool = await _sample_pool(db, epoch.id)
        issued = await _issued_ids(db)
        made: list[uuid.UUID] = []
        J = LandscapeExpertJudgment
        for expert in experts:
            if await db.scalar(select(exists().where(J.expert_id == expert.id, J.quarter == quarter))):
                continue
            engaged = await _engaged(db, expert.id)
            open_items = sorted((o for o in pool if o not in engaged), key=str)
            for opportunity_id in _rng.sample(open_items, min(EXPERT_SAMPLE_SIZE, len(open_items))):
                row = J(
                    id=uuid.uuid4(), opportunity_id=opportunity_id, expert_id=expert.id,
                    expert_name=expert.name, quarter=quarter,
                    landscape_assessment_id=pool[opportunity_id],
                    issued_before_draw=opportunity_id in issued,
                )
                db.add(row)
                made.append(row.id)
        await db.commit()
    return made
