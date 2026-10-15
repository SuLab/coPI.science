"""restore_publication: a restored system exclusion sticks (becomes manual); a staff
exclusion keeps its provenance."""
from datetime import UTC, datetime

import pytest
from sqlalchemy import func

from src.models import USER_ROLE_MANAGER, Publication
from src.models.publication import SYSTEM_EXCLUDED_REASONS
from src.services.corpus import CorpusResult
from src.services.corpus_additions import apply_corpus_result
from src.services.profile_review import exclude_publication, restore_publication
from src.services.user_deletion import delete_user_account
from tests import factories

pytestmark = pytest.mark.integration


def _rec(pmid, stages=("s1",), **kw):
    return {"pmid": pmid, "title": f"Paper {pmid}", "abstract": "A.", "journal": "J",
            "year": 2020, "doi": None, "pmcid": None, "stages": list(stages), **kw}


async def test_restored_system_exclusion_becomes_manual_and_is_not_re_excluded(db_session):
    pi = await factories.make_user(db_session, name="Ada Lovelace")
    pre = Publication(user_id=pi.id, pmid="1", title="Pre", provenance="unanchored",
                      excluded_at=func.now())
    pub = Publication(user_id=pi.id, pmid="2", title="Pub", provenance="s1")
    db_session.add_all([pre, pub])
    await db_session.flush()

    restored = await restore_publication(db_session, pi.id, pre.id)
    assert restored is not None and restored.excluded_at is None
    assert restored.provenance == "manual"

    result = CorpusResult(kept=[], flagged=[], ranked=[_rec("2")])
    result.superseded = [("1", "2")]
    outcome = await apply_corpus_result(db_session, pi.id, result)
    await db_session.refresh(pre)
    assert outcome.superseded_excluded == [] and pre.excluded_at is None


async def test_restored_staff_exclusion_keeps_its_provenance(db_session):
    pi = await factories.make_user(db_session, name="Ada Lovelace")
    staff = await factories.make_user(db_session, name="Staff Member")
    row = Publication(user_id=pi.id, pmid="1", title="T", provenance="s1",
                      excluded_at=func.now(), excluded_by_user_id=staff.id)
    db_session.add(row)
    await db_session.flush()

    restored = await restore_publication(db_session, pi.id, row.id)
    assert restored is not None and restored.excluded_at is None
    assert restored.excluded_by_user_id is None and restored.provenance == "s1"


async def test_restore_of_a_staff_exclusion_keeps_provenance_after_the_excluder_is_deleted(
    db_session,
):
    pi = await factories.make_user(db_session, name="Ada Lovelace")
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    row = Publication(user_id=pi.id, pmid="1", title="T", provenance="s1,s4")
    db_session.add(row)
    await db_session.flush()
    await exclude_publication(db_session, pi.id, row.id, actor_id=manager.id)
    await delete_user_account(db_session, manager)
    await db_session.refresh(row)
    assert (row.excluded_by_user_id, row.excluded_reason) == (None, "staff")
    restored = await restore_publication(db_session, pi.id, row.id)
    assert restored.provenance == "s1,s4"
    assert (restored.excluded_at, restored.excluded_by_user_id, restored.excluded_reason) == (
        None, None, None)


@pytest.mark.parametrize("reason", sorted(SYSTEM_EXCLUDED_REASONS))
async def test_restore_of_a_system_reason_turns_the_row_manual(db_session, reason):
    pi = await factories.make_user(db_session, name="Ada Lovelace")
    row = Publication(user_id=pi.id, pmid="1", title="T", provenance="s1",
                      excluded_at=datetime.now(UTC), excluded_reason=reason)
    db_session.add(row)
    await db_session.flush()
    restored = await restore_publication(db_session, pi.id, row.id)
    assert (restored.provenance, restored.excluded_reason) == ("manual", None)


async def test_restore_of_a_legacy_exclusion_follows_excluded_by(db_session):
    pi = await factories.make_user(db_session, name="Ada Lovelace")
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    a = Publication(user_id=pi.id, pmid="1", title="A", provenance="s1",
                    excluded_at=datetime.now(UTC))
    b = Publication(user_id=pi.id, pmid="2", title="B", provenance="s1",
                    excluded_at=datetime.now(UTC), excluded_by_user_id=manager.id)
    db_session.add_all([a, b])
    await db_session.flush()
    assert (await restore_publication(db_session, pi.id, a.id)).provenance == "manual"
    assert (await restore_publication(db_session, pi.id, b.id)).provenance == "s1"
