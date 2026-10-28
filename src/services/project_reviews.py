"""Project reviews: a 1-5 rating, a comment, and fund or don't fund (spec §5.11).

Kept apart from assessment reviews: nothing here touches ``assessment_reviews``, and no bot,
Path A or assessment count reads these rows. One review per person per project
(``uq_hub_project_reviews_project_reviewer``); the author edits it and an admin deletes it.
Callers commit. The comment rules are the assessment reviews' (``_validate_comment``).

Review assignments (spec §7) mirror the assessment ones (``assign_reviewer``): staff name who
should review a project; the router validates the assignee and refuses impersonation.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import User
from src.models.hub_project import HubProject, HubProjectReview, HubProjectReviewAssignment
from src.services.assessment_reviews import _validate_comment

_UNIQUE_CONSTRAINT = "uq_hub_project_reviews_project_reviewer"
_ASSIGNMENT_CONSTRAINT = "uq_hub_project_review_assignment_once"


class DuplicateProjectReviewError(Exception):
    """This reviewer has already reviewed this project; nothing was written."""


def _validate(rating: int, comment: str) -> None:
    """ValueError for a rating outside 1-5 or a comment ``_validate_comment`` refuses."""
    if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
        raise ValueError("rating must be a whole number from 1 to 5")
    _validate_comment(comment)


async def submit_project_review(
    db: AsyncSession,
    *,
    project: HubProject,
    reviewer: User,
    rating: int,
    comment: str,
    fund: bool,
) -> HubProjectReview:
    """Insert ``reviewer``'s review of ``project``, keeping the author's name.

    The insert runs in a savepoint, so two racing submits by the same person leave one row:
    the loser's unique-constraint violation becomes ``DuplicateProjectReviewError`` and the
    caller's transaction stays usable.
    """
    _validate(rating, comment)
    review = HubProjectReview(
        project_id=project.id,
        reviewer_user_id=reviewer.id,
        reviewer_name=reviewer.name,
        rating=rating,
        comment=comment,
        fund=fund,
    )
    try:
        async with db.begin_nested():
            db.add(review)
            await db.flush()
    except IntegrityError as exc:
        if _UNIQUE_CONSTRAINT in str(exc.orig):
            raise DuplicateProjectReviewError from exc
        raise
    return review


async def edit_project_review(
    db: AsyncSession, *, review: HubProjectReview, rating: int, comment: str, fund: bool
) -> None:
    """Replace the review's rating, comment and fund choice. The caller checks authorship."""
    _validate(rating, comment)
    review.rating = rating
    review.comment = comment
    review.fund = fund
    review.updated_at = datetime.now(UTC)
    await db.flush()


async def delete_project_review(db: AsyncSession, *, review: HubProjectReview) -> None:
    """Remove the review. The caller checks that the actor is an admin."""
    await db.delete(review)
    await db.flush()


async def assign_project_reviewer(
    db: AsyncSession, *, project: HubProject, assignee: User, assigned_by: User
) -> None:
    """Record ``assignee`` as assigned to review ``project``; a repeat is a no-op.

    The caller validates that the assignee is review-capable and allowed. The conflict target
    names the unique constraint, as ``assign_reviewer`` does for assessments.
    """
    await db.execute(
        pg_insert(HubProjectReviewAssignment.__table__)
        .values(
            project_id=project.id,
            assignee_user_id=assignee.id,
            assignee_name=assignee.name,
            assigned_by_user_id=assigned_by.id,
            assigned_by_name=assigned_by.name,
        )
        .on_conflict_do_nothing(constraint=_ASSIGNMENT_CONSTRAINT)
    )


async def unassign_project_reviewer(
    db: AsyncSession, *, project: HubProject, assignee_user_id: uuid.UUID
) -> None:
    """Remove the (project, assignee) assignment if there is one; none is not an error."""
    await db.execute(
        delete(HubProjectReviewAssignment).where(
            HubProjectReviewAssignment.project_id == project.id,
            HubProjectReviewAssignment.assignee_user_id == assignee_user_id,
        )
    )


async def project_assignments(
    db: AsyncSession, project_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[HubProjectReviewAssignment]]:
    """Each listed project's assignments, oldest first; a project with none is absent."""
    ids = list(project_ids)
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(HubProjectReviewAssignment)
            .where(HubProjectReviewAssignment.project_id.in_(ids))
            .order_by(HubProjectReviewAssignment.created_at, HubProjectReviewAssignment.id)
        )
    ).scalars()
    result: dict[uuid.UUID, list[HubProjectReviewAssignment]] = {}
    for row in rows:
        result.setdefault(row.project_id, []).append(row)
    return result
