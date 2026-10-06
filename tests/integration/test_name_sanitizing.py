"""ORCID- and OAuth-sourced names are cut to the D60 allowlist and flagged (spec 2026-10-05
§4.1, D60); an iD name is stored as today until Phase 3."""
import pytest

from src.routers import auth
from src.services import pi_onboarding
from tests import factories

pytestmark = pytest.mark.integration


async def test_add_pi_cuts_a_stray_symbol_and_flags_it(db_session, monkeypatch):
    async def fake_profile(orcid):
        return {"name": "Jane Doe™", "orcid": orcid, "employments": []}

    monkeypatch.setattr(pi_onboarding, "fetch_orcid_profile", fake_profile)
    user = await pi_onboarding.find_or_create_pi_by_orcid(db_session, "0000-0002-1825-0097")
    assert user.name == "Jane Doe" and user.name_sanitized_at is not None


async def test_add_pi_keeps_an_orcid_id_name_unflagged(db_session, monkeypatch):
    async def fake_profile(orcid):
        return {"name": orcid, "orcid": orcid, "employments": []}

    monkeypatch.setattr(pi_onboarding, "fetch_orcid_profile", fake_profile)
    user = await pi_onboarding.find_or_create_pi_by_orcid(db_session, "0000-0002-1825-0097")
    assert user.name == "0000-0002-1825-0097" and user.name_sanitized_at is None


async def test_the_agent_pi_name_copies_a_human_typed_name_unflagged(db_session):
    # create_pending_agent_for never rewrites users.name, so it neither cuts pi_name nor
    # stamps name_sanitized_at; machine-sourced names were cut where they entered users.
    user = await factories.make_user(db_session, name="Jane Doe™")
    agent = await pi_onboarding.create_pending_agent_for(db_session, user)
    assert agent.pi_name == "Jane Doe™" and user.name == "Jane Doe™"
    assert user.name_sanitized_at is None


async def test_add_pi_strips_newlines_and_markdown_from_an_id_name(db_session, monkeypatch):
    async def fake_profile(orcid):
        return {"name": f"  {orcid}\n**#", "orcid": orcid, "employments": []}

    monkeypatch.setattr(pi_onboarding, "fetch_orcid_profile", fake_profile)
    user = await pi_onboarding.find_or_create_pi_by_orcid(db_session, "0000-0002-1825-0097")
    assert user.name == "0000-0002-1825-0097" and user.name_sanitized_at is not None


async def test_first_login_sanitises_the_oauth_name(db_session):
    user = await auth._create_new_user(
        db_session, orcid_id="0000-0002-1825-0098", orcid_name="Jane Doe™",
        profile_data={}, allowlist_entry=None, resolved_email=None,
    )
    assert user.name == "Jane Doe" and user.name_sanitized_at is not None
