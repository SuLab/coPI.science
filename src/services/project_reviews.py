"""Project reviews: a 1-5 rating, a comment, and fund or don't fund (spec §5.11).

Kept apart from assessment reviews: nothing here touches ``assessment_reviews``, and no bot,
Path A or assessment count reads these rows. One review per person per project
(``uq_hub_project_reviews_project_reviewer``); the author edits it and an admin deletes it.
Callers commit. The comment rules are the assessment reviews' (``_validate_comment``).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import User
from src.models.hub_project import HubProject, HubProjectReview
from src.services.assessment_reviews import _validate_comment

_UNIQUE_CONSTRAINT = "uq_hub_project_reviews_project_reviewer"


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
