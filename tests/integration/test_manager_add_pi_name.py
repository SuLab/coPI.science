"""POST /workspace/pis with a nameless ORCID record (spec 2026-10-05 §6.3 Names)."""
import pytest
from sqlalchemy import select

from src.models import USER_ROLE_MANAGER, User
from src.services import persona_lifecycle, pi_onboarding, profile_export
from tests import factories
from tests.integration.test_manager_access import auth_headers

pytestmark = pytest.mark.integration
_OID = "0000-0002-1825-0501"


@pytest.fixture
def nameless(monkeypatch, tmp_path):
    async def fetch(orcid):
        return {"orcid": orcid, "name": None, "employments": []}

    monkeypatch.setattr(pi_onboarding, "fetch_orcid_profile", fetch)
    # Add-PI's post-commit lifecycle export archives a file left at the new slug.
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path / "public")
    monkeypatch.setattr(persona_lifecycle, "ORPHANED_DIR", tmp_path / "orphaned")


async def test_a_nameless_record_without_a_name_is_refused(client, db_session, nameless):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    r = await client.post("/workspace/pis", data={"orcid": _OID}, headers=auth_headers(mgr.id),
                          follow_redirects=False)
    assert "error=name_required" in r.headers["location"] and _OID in r.headers["location"]
    assert (await db_session.execute(select(User).where(User.orcid == _OID))).scalar_one_or_none() is None


async def test_a_nameless_record_with_a_name_is_created(client, db_session, nameless):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    r = await client.post("/workspace/pis", data={"orcid": _OID, "name": "Ada Lovelace"},
                          headers=auth_headers(mgr.id), follow_redirects=False)
    assert r.status_code == 302 and "error" not in r.headers["location"]
    user = (await db_session.execute(select(User).where(User.orcid == _OID))).scalar_one()
    assert user.name == "Ada Lovelace"


async def test_an_invalid_name_is_refused(client, db_session, nameless):
    mgr = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    r = await client.post("/workspace/pis", data={"orcid": _OID, "name": "Ada <b>"},
                          headers=auth_headers(mgr.id), follow_redirects=False)
    assert "error=invalid_name" in r.headers["location"]
