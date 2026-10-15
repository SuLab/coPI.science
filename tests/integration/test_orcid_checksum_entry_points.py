"""R2.2: a wrong ORCID check digit is refused at every entry point that takes a typed iD."""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from src.models import USER_ROLE_ADMIN, USER_ROLE_MANAGER, USER_ROLE_REVIEWER, AccessAllowlist, User
from src.services import staff_accounts
from src.services.staff_accounts import StaffAccountRefused
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration

WRONG = "0000-0002-1825-0098"  # 0000-0002-1825-0097 with its last digit mistyped


async def test_add_pi_refuses_a_wrong_check_digit_before_any_fetch(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    fetch = AsyncMock()
    with patch("src.services.pi_onboarding.fetch_orcid_profile", new=fetch):
        r = await client.post("/workspace/pis", data={"orcid": WRONG},
                              headers=auth_headers(manager.id), follow_redirects=False)
    assert r.status_code == 302
    assert "error=invalid_orcid" in r.headers["location"]
    assert (await db_session.execute(select(User).where(User.orcid == WRONG))).scalar_one_or_none() is None
    fetch.assert_not_awaited()


async def test_staff_preview_refuses_a_wrong_check_digit(db_session, monkeypatch):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    fetch = AsyncMock()
    monkeypatch.setattr(staff_accounts, "fetch_orcid_profile", fetch)
    with pytest.raises(StaffAccountRefused) as exc:
        await staff_accounts.preview(db_session, actor=admin, orcid=WRONG, role=USER_ROLE_REVIEWER)
    assert exc.value.code == "invalid_orcid"
    fetch.assert_not_awaited()


async def test_impersonating_a_wrong_check_digit_is_404(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    r = await client.post("/admin/impersonate", data={"orcid": WRONG},
                          headers=auth_headers(admin.id), follow_redirects=False)
    assert r.status_code == 404
    assert "No user has that ORCID iD" in r.text


async def test_allowlisting_a_wrong_check_digit_adds_nothing(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    before = await db_session.scalar(select(func.count()).select_from(AccessAllowlist))
    r = await client.post("/admin/access-allowlist/add", data={"orcid": WRONG, "note": ""},
                          headers=auth_headers(admin.id), follow_redirects=False)
    assert r.status_code == 302
    assert await db_session.scalar(select(func.count()).select_from(AccessAllowlist)) == before
