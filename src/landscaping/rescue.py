"""Staff rescue of a filtered item, and its carry into later epochs (spec §10.5; L43, L44).

A rescue sets the excluded stage row's status to ``rescued`` with a compare-and-set on the
status the staff member saw, so two staff cannot double it. The item then competes normally
from the next stage and is given no slot:

| ``item_kind`` | ``stage`` | Rescues | Then |
|---|---|---|---|
| ``screen`` | ``screen`` | S2 ``excluded:screen`` | the document goes to S3 |
| ``triage`` | ``triage`` | S4 ``excluded:<reason>`` | the idea joins the S5 pool |
| ``deep`` | ``deep_gate`` | S6 ``excluded:deep_gate`` | the idea joins S7 and S8 while in the cut |

Every rescue is a ``LandscapeRescue`` row with its actor and reason. That row is how a rescue
joins the regression set: ``eval.regression.freeze_regression_set`` adds every rescued item to
the next set version (§10.4).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.landscape import (
    LandscapeDeep,
    LandscapeIdea,
    LandscapeRescue,
    LandscapeScreen,
    LandscapeTriage,
)
from src.models.user import User


class RescueConflict(Exception):
    """The row's status is no longer the one the rescuer saw (another rescue, a re-run)."""


@dataclass(frozen=True)
class _Kind:
    model: type
    stage: str
    #: The one exclusion status this stage rescues; None accepts any ``excluded:<reason>``.
    exclusion: str | None

    def excludes(self, status: str) -> bool:
        if self.exclusion is not None:
            return status == self.exclusion
        return status.startswith("excluded:")

    def excluded_clause(self) -> Any:
        if self.exclusion is not None:
            return self.model.status == self.exclusion
        return self.model.status.startswith("excluded:")


_KINDS = {
    "screen": _Kind(LandscapeScreen, "screen", "excluded:screen"),
    "triage": _Kind(LandscapeTriage, "triage", None),
    "deep": _Kind(LandscapeDeep, "deep_gate", "excluded:deep_gate"),
}


async def _set_rescued(
    db: AsyncSession, kind: _Kind, row_id: uuid.UUID, expected_status: str
) -> uuid.UUID | None:
    """``UPDATE … SET status = 'rescued' WHERE id = row_id AND status = expected_status``;
    the row's epoch, or None when nothing changed."""
    table = kind.model.__table__
    return await db.scalar(
        update(table)
        .where(table.c.id == row_id, table.c.status == expected_status)
        .values(status="rescued", updated_at=func.now())
        .returning(table.c.epoch_id)
    )


async def rescue(
    db: AsyncSession, *, item_kind: str, item_id: uuid.UUID, stage: str, expected_status: str,
    actor: User, reason: str,
) -> None:
    """Rescue one excluded stage row (L43) and record it; commits.

    ``ValueError`` when the reason is blank, the kind and stage do not belong together (see
    the module table), or ``expected_status`` is not that stage's exclusion. ``RescueConflict``
    when the row's status is no longer ``expected_status``; nothing is recorded then.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a rescue needs a reason")
    kind = _KINDS.get(item_kind)
    if kind is None or kind.stage != stage:
        raise ValueError(f"cannot rescue a {item_kind!r} row at stage {stage!r}")
    if not kind.excludes(expected_status):
        raise ValueError(f"{expected_status!r} is not an exclusion at stage {stage!r}")
    epoch_id = await _set_rescued(db, kind, item_id, expected_status)
    if epoch_id is None:
        raise RescueConflict(f"{item_kind} row {item_id} is no longer {expected_status!r}")
    db.add(LandscapeRescue(
        item_kind=item_kind, item_id=item_id, stage=stage, epoch_id=epoch_id,
        actor_id=actor.id, actor_name=actor.name, reason=reason,
    ))
    await db.commit()


# --- carrying rescues into a later epoch ----------------------------------------------------


async def _identities(
    db: AsyncSession, kind: _Kind, item_kind: str, epoch_id: uuid.UUID
) -> dict[tuple, LandscapeRescue]:
    """The epoch's rescues of ``item_kind``, keyed by the item's identity across epochs: a
    document id, or an idea's ``(idea_key, idea_version)``."""
    model, rescues = kind.model, LandscapeRescue
    if model is LandscapeScreen:
        query = select(rescues, model.document_id).join(model, model.id == rescues.item_id)
    else:
        query = (
            select(rescues, LandscapeIdea.idea_key, model.idea_version)
            .join(model, model.id == rescues.item_id)
            .join(LandscapeIdea, LandscapeIdea.id == model.idea_id)
        )
    rows = (
        await db.execute(
            query.where(rescues.epoch_id == epoch_id, rescues.item_kind == item_kind)
            .order_by(rescues.created_at)
        )
    ).all()
    return {tuple(row)[1:]: row[0] for row in rows}


async def _excluded_again(
    db: AsyncSession, kind: _Kind, epoch_id: uuid.UUID
) -> list[tuple[tuple, uuid.UUID, str]]:
    """``(identity, row id, status)`` of every row of the epoch excluded at ``kind``'s stage."""
    model = kind.model
    if model is LandscapeScreen:
        query = select(model.document_id, model.id, model.status)
    else:
        query = (
            select(LandscapeIdea.idea_key, model.idea_version, model.id, model.status)
            .join(LandscapeIdea, LandscapeIdea.id == model.idea_id)
        )
    rows = (
        await db.execute(query.where(model.epoch_id == epoch_id, kind.excluded_clause()))
    ).all()
    return [(tuple(row)[:-2], row[-2], row[-1]) for row in rows]


async def carry_rescues(
    db: AsyncSession, *, from_epoch: uuid.UUID, to_epoch: uuid.UUID
) -> int:
    """Re-apply ``from_epoch``'s rescues in ``to_epoch`` (spec §10.5); commits.

    An item ``from_epoch`` rescued (carried rescues included) that ``to_epoch`` has excluded
    again at the same stage is rescued again, recorded as carried with the original actor and
    reason. The same item is the same document (S2), or the same idea key and version (S4, S6).
    Items not yet excluded in ``to_epoch`` are left alone, so a call each loop of the re-judge
    picks them up as their exclusions land. Returns the number carried.
    """
    carried = 0
    for item_kind, kind in _KINDS.items():
        rescued = await _identities(db, kind, item_kind, from_epoch)
        if not rescued:
            continue
        for identity, row_id, status in await _excluded_again(db, kind, to_epoch):
            original = rescued.get(identity)
            if original is None or await _set_rescued(db, kind, row_id, status) is None:
                continue
            db.add(LandscapeRescue(
                item_kind=item_kind, item_id=row_id, stage=kind.stage, epoch_id=to_epoch,
                actor_id=original.actor_id, actor_name=original.actor_name,
                reason=original.reason, carried=True,
            ))
            carried += 1
    await db.commit()
    return carried
