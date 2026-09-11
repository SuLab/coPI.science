"""Unit tests for the in-run publication dedup fix."""

import pytest
from sqlalchemy import select

from src.models import Publication
from src.services.profile_pipeline import _dedup_pmids, _insert_publication_tolerating_conflict
from tests import factories

pytestmark = pytest.mark.integration


def test_pmids_are_deduplicated_across_orcid_works_preserving_order():
    pmids, seen = _dedup_pmids(
        [{"pmid": "111"}, {"pmid": "111"}, {"pmid": "222"}, {"pmid": None}, {}]
    )
    assert pmids == ["111", "222"]
    assert seen == {"111", "222"}


async def test_a_concurrent_writer_inserting_the_same_pmid_does_not_raise(db_session):
    """The uq_publications_user_pmid constraint (migration 0025) makes two
    overlapping pipeline runs for one user race on this INSERT. The second
    writer's `await db.flush()` must not raise IntegrityError and fail the
    whole job instead of just recognizing the row already exists.
    Simulated here as two inserts of the SAME (user_id, pmid) in one session,
    which is the same conflict a second session's committed row would produce."""
    user = await factories.make_user(db_session, name="Concurrent Lovelace")

    first, first_inserted = await _insert_publication_tolerating_conflict(
        db_session, user.id, "5001", title="First writer's title", abstract="A",
    )
    assert first_inserted is True

    second, second_inserted = await _insert_publication_tolerating_conflict(
        db_session, user.id, "5001", title="Second (losing) writer's title", abstract="B",
    )
    assert second_inserted is False
    assert second.id == first.id  # the loser is handed the winner's row, not a crash

    rows = (
        await db_session.execute(
            select(Publication).where(Publication.user_id == user.id, Publication.pmid == "5001")
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].title == "First writer's title"  # the winner's insert is what landed


async def test_the_happy_path_still_inserts_with_no_conflict(db_session):
    user = await factories.make_user(db_session, name="Solo Lovelace")

    pub, inserted = await _insert_publication_tolerating_conflict(
        db_session, user.id, "5002", title="T", abstract="A",
    )
    assert inserted is True
    assert pub.pmid == "5002"

    rows = (
        await db_session.execute(
            select(Publication).where(Publication.user_id == user.id, Publication.pmid == "5002")
        )
    ).scalars().all()
    assert len(rows) == 1
