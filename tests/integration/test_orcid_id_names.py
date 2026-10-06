"""ORCID-iD names (spec 2026-10-05 §6.3 Names): login replaces one, Add-PI asks for a name
when ORCID has none, an adopted account's empty or iD name is replaced. The CLI refusals
are in tests/integration/test_cli.py (the CLI commits on its own engine)."""
import pytest

from src.routers import auth as auth_router
from src.services import pi_onboarding
from src.services.person_names import InvalidPersonName
from tests import factories

pytestmark = pytest.mark.integration


async def test_login_replaces_an_orcid_id_name_with_the_oauth_name(db_session):
    user = await factories.make_user(db_session, name="0000-0002-1825-0401",
                                     orcid="0000-0002-1825-0401")
    got = await auth_router._find_or_create_user(
        db_session, orcid_id=user.orcid, orcid_name="Ada Lovelace",
        profile_data={"orcid": user.orcid, "name": None}, allowlist_entry=None,
    )
    assert got.id == user.id and got.name == "Ada Lovelace"


async def test_login_keeps_a_real_name(db_session):
    user = await factories.make_user(db_session, name="Ada King", orcid="0000-0002-1825-0402")
    got = await auth_router._find_or_create_user(
        db_session, orcid_id=user.orcid, orcid_name="Someone Else",
        profile_data={"orcid": user.orcid, "name": "Ada Lovelace"}, allowlist_entry=None,
    )
    assert got.name == "Ada King"


async def test_add_pi_requires_a_name_when_orcid_has_none(db_session, monkeypatch):
    async def nameless(orcid):
        return {"orcid": orcid, "name": None, "employments": []}

    monkeypatch.setattr(pi_onboarding, "fetch_orcid_profile", nameless)
    with pytest.raises(pi_onboarding.OrcidNameRequired):
        await pi_onboarding.find_or_create_pi_by_orcid(db_session, "0000-0002-1825-0403")
    with pytest.raises(InvalidPersonName):
        await pi_onboarding.find_or_create_pi_by_orcid(db_session, "0000-0002-1825-0403",
                                                       name="<script>")
    user = await pi_onboarding.find_or_create_pi_by_orcid(db_session, "0000-0002-1825-0403",
                                                          name="Ada Lovelace")
    assert user.name == "Ada Lovelace"


async def test_add_pi_ignores_the_entered_name_when_orcid_has_one(db_session, monkeypatch):
    async def named(orcid):
        return {"orcid": orcid, "name": "Ada Lovelace", "employments": []}

    monkeypatch.setattr(pi_onboarding, "fetch_orcid_profile", named)
    user = await pi_onboarding.find_or_create_pi_by_orcid(db_session, "0000-0002-1825-0404",
                                                          name="Someone Else")
    assert user.name == "Ada Lovelace"


async def _nameless_record(orcid):
    return {"orcid": orcid, "name": None, "employments": []}


@pytest.mark.parametrize("stored", ["0000-0002-1825-0405", "", "   "])
async def test_adopt_replaces_an_id_or_empty_name(db_session, monkeypatch, stored):
    """Amendment 7b: an empty name is replaced exactly as an iD-like one is."""
    monkeypatch.setattr(pi_onboarding, "fetch_orcid_profile", _nameless_record)
    user = await factories.make_user(db_session, name=stored, orcid="0000-0002-1825-0405",
                                     access_status="pending")
    got = await pi_onboarding.adopt_agentless_pi(db_session, user.orcid, name="Ada Lovelace")
    assert got.id == user.id and got.name == "Ada Lovelace"


async def test_adopt_keeps_a_real_name(db_session, monkeypatch):
    monkeypatch.setattr(pi_onboarding, "fetch_orcid_profile", _nameless_record)
    user = await factories.make_user(db_session, name="Ada King", orcid="0000-0002-1825-0406",
                                     access_status="pending")
    got = await pi_onboarding.adopt_agentless_pi(db_session, user.orcid, name="Ada Lovelace")
    assert got.name == "Ada King"


async def test_adopt_validates_the_entered_name(db_session, monkeypatch):
    monkeypatch.setattr(pi_onboarding, "fetch_orcid_profile", _nameless_record)
    user = await factories.make_user(db_session, name="0000-0002-1825-0407",
                                     orcid="0000-0002-1825-0407", access_status="pending")
    with pytest.raises(InvalidPersonName):
        await pi_onboarding.adopt_agentless_pi(db_session, user.orcid, name="<script>")
