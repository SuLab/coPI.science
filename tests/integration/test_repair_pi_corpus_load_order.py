"""scripts/repair_pi_corpus.py — which duplicate-PMID row is removed.

``partition_duplicate_pmids`` keeps the first row it sees per PMID, so the
removal is decided by ``load_stored_publications``' ORDER BY. Ordering by the
random UUID ``id`` alone made that choice arbitrary; ordering by
``created_at`` first keeps the row that was stored first and removes the
later-added copy.

``--only duplicate-pmids`` stays as the pre-0056 repair, so the duplicate state
is built by dropping ``uq_publications_user_pmid`` inside the test's rolled-back
transaction (migration 0056 makes the state otherwise unreachable).
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from scripts.repair_pi_corpus import load_stored_publications, partition_duplicate_pmids
from src.models import Publication
from tests import factories

pytestmark = pytest.mark.integration


async def test_the_later_added_duplicate_is_the_one_removed(db_session):
    user = await factories.make_user(db_session)
    await db_session.execute(
        text("ALTER TABLE publications DROP CONSTRAINT uq_publications_user_pmid")
    )
    older_id = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    newer_id = uuid.UUID("00000000-0000-0000-0000-000000000000")
    # The ids are chosen so that id order is the REVERSE of created_at order:
    # an id-only ORDER BY would keep the newer row and remove the older one.
    db_session.add_all(
        [
            Publication(
                id=older_id, user_id=user.id, pmid="31000000", title="Same paper",
                year=2020, created_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            Publication(
                id=newer_id, user_id=user.id, pmid="31000000", title="Same paper",
                year=2020, created_at=datetime(2026, 2, 1, tzinfo=UTC),
            ),
        ]
    )
    await db_session.flush()

    stored = await load_stored_publications(db_session, user.id)
    removals, canonical = partition_duplicate_pmids(stored)

    assert [r.publication_id for r in removals] == [str(newer_id)]
    assert removals[0].evidence == {"duplicate_of_publication_id": str(older_id)}
    assert [p.id for p in canonical] == [str(older_id)]
