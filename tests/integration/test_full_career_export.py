"""scoped_publications_for_export follows researcher_profiles.full_career_fallback
(spec 2026-10-09 §4.4)."""
from datetime import UTC, datetime

import pytest

from src.models import Publication
from src.services.jhu_rules import set_tenure_start
from src.services.tenure_scope import scoped_publications_for_export
from tests import factories

pytestmark = pytest.mark.integration


async def _pi(db_session, flag):
    user = await factories.make_user(db_session)
    await factories.make_profile(db_session, user=user, full_career_fallback=flag)
    await set_tenure_start(user.id, 2020, "manual", db=db_session)
    db_session.add_all([
        Publication(user_id=user.id, pmid="1", title="JHU-era", year=2021),
        Publication(user_id=user.id, pmid="2", title="Earlier", year=2015),
    ])
    await db_session.flush()
    return user


@pytest.mark.parametrize("flag", [None, False])
async def test_without_the_flag_the_export_keeps_the_tenure_window(db_session, flag):
    user = await _pi(db_session, flag)
    scoped = await scoped_publications_for_export(db_session, user.id)
    assert [p.title for p in scoped] == ["JHU-era"]
    assert scoped.tenure_start == 2020


async def test_with_the_flag_the_export_is_the_full_career(db_session):
    user = await _pi(db_session, True)
    scoped = await scoped_publications_for_export(db_session, user.id)
    assert [p.title for p in scoped] == ["JHU-era", "Earlier"]
    assert scoped.tenure_start is None


async def test_excluded_rows_stay_out_of_a_full_career_export(db_session):
    user = await _pi(db_session, True)
    db_session.add(Publication(user_id=user.id, pmid="3", title="Excluded", year=2010,
                               excluded_at=datetime.now(UTC)))
    await db_session.flush()
    scoped = await scoped_publications_for_export(db_session, user.id)
    assert "Excluded" not in [p.title for p in scoped]


async def test_a_pi_with_no_profile_exports_by_the_tenure_window(db_session):
    user = await factories.make_user(db_session)
    await set_tenure_start(user.id, 2020, "manual", db=db_session)
    db_session.add(Publication(user_id=user.id, pmid="9", title="Earlier", year=2015))
    await db_session.flush()
    assert list(await scoped_publications_for_export(db_session, user.id)) == []
