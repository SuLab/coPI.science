"""The directory service. The extracted queries are pinned by the existing
characterization tests; what is new here is the `roles` filter, which is how
one function serves both /admin (no filter) and /manager (PIs only, D11).
"""

import pytest

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI
from src.services.directory import list_pi_directory
from tests import factories

pytestmark = pytest.mark.integration


async def test_roles_none_returns_every_account_type(db_session):
    await factories.make_user(db_session, user_role=USER_ROLE_PI)
    await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    rows = await list_pi_directory(db_session)
    assert sorted(r["user"].user_role for r in rows) == [
        USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_PI,
    ]


async def test_roles_pi_excludes_staff_accounts(db_session):
    await factories.make_user(db_session, user_role=USER_ROLE_PI)
    await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    rows = await list_pi_directory(db_session, roles=(USER_ROLE_PI,))
    assert [r["user"].user_role for r in rows] == [USER_ROLE_PI]


async def test_unclaimed_pi_stubs_are_included(db_session):
    """D11: managers see recruitment coverage, so a seeded-but-never-signed-in
    PI (claimed_at=None, no profile) must still appear."""
    await factories.make_user(
        db_session, user_role=USER_ROLE_PI, claimed_at=None, onboarding_complete=False
    )
    rows = await list_pi_directory(db_session, roles=(USER_ROLE_PI,))
    assert len(rows) == 1
    assert rows[0]["profile_status"] == "no_profile"


async def test_load_user_detail_returns_none_for_a_missing_row(db_session):
    import uuid

    from src.services.directory import load_user_detail
    assert await load_user_detail(db_session, uuid.uuid4()) is None
