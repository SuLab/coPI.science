"""src/services/pi_companies.py against the database (spec 2026-10-02 §7.2, §7.5): manual
entries are confirmed at creation, every write validates, a normalized name is unique per
PI in every status, and confirm and reject act only on suggestions."""

import uuid
from datetime import date

import pytest

from src.models import USER_ROLE_MANAGER
from src.services.pi_companies import (
    CompanyNotFoundError,
    CompanyValidationError,
    add_company,
    confirm_company,
    confirmed_companies_for_agent,
    delete_company,
    list_companies,
    reject_company,
)
from tests import factories
from tests.pi_company_support import EDGAR_URL, companies_dir, seed_company

pytestmark = pytest.mark.integration

_GOOD = dict(
    company_name="Belay Diagnostics", pi_role="co_founder", funding_usd=5_000_000,
    funding_as_of=date(2024, 1, 2), source_url="https://example.org/belay",
)


@pytest.fixture(autouse=True)
def _companies_dir(monkeypatch, tmp_path):
    """Every write here also re-exports: never into the live profiles/ tree."""
    return companies_dir(monkeypatch, tmp_path)


async def _pi_agent_manager(db_session):
    pi = await factories.make_user(db_session, name="Victor Velculescu")
    agent = await factories.make_agent(db_session, user=pi)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    return pi, agent, manager


async def test_a_manual_entry_is_confirmed_and_reviewed_by_its_creator(db_session):
    pi, _agent, manager = await _pi_agent_manager(db_session)
    row = await add_company(
        db_session, user_id=pi.id, created_by_user_id=manager.id,
        **{**_GOOD, "company_name": "  Belay Diagnostics, Inc. "},
    )
    assert (row.company_name, row.normalized_name) == ("Belay Diagnostics, Inc.", "belay diagnostics")
    assert (row.status, row.origin, row.evidence, row.funding_source_url) == (
        "confirmed", "manual", None, None)
    assert row.created_by_user_id == row.reviewed_by_user_id == manager.id
    assert row.reviewed_at == row.created_at
    assert [r.id for r in await list_companies(db_session, pi.id)] == [row.id]


@pytest.mark.parametrize("overrides,message", [
    ({"company_name": "   "}, "Enter the company"),
    ({"company_name": "x" * 201}, "limited to 200 characters"),
    ({"company_name": "Acme\nBio"}, "one line"),
    ({"company_name": "!!!"}, "at least one letter or digit"),
    ({"pi_role": "ceo"}, "Choose the PI"),
    ({"source_url": ""}, "Give a source link"),
    ({"source_url": "javascript:alert(1)"}, "http:// or https://"),
    ({"source_url": "ftp://example.org/x"}, "http:// or https://"),
    ({"source_url": "https://exa mple.org"}, "no spaces"),
    ({"source_url": "https://"}, "http:// or https://"),
    ({"funding_usd": -1}, "cannot be negative"),
    ({"funding_usd": 2**63}, "too large"),
    ({"funding_as_of": None}, "as of"),
    ({"funding_usd": None}, "Leave the as-of date blank"),
])
async def test_a_bad_entry_is_refused_and_nothing_is_written(db_session, overrides, message):
    pi, _agent, manager = await _pi_agent_manager(db_session)
    with pytest.raises(CompanyValidationError, match=message):
        await add_company(
            db_session, user_id=pi.id, created_by_user_id=manager.id, **{**_GOOD, **overrides},
        )
    assert await list_companies(db_session, pi.id) == []


@pytest.mark.parametrize("status,message", [
    ("confirmed", "already on this PI"),
    ("suggested", "already suggested for this PI"),
    ("rejected", "was rejected for this PI earlier"),
])
async def test_a_name_the_pi_already_has_is_refused_in_every_status(db_session, status, message):
    pi, _agent, manager = await _pi_agent_manager(db_session)
    await seed_company(db_session, pi, company_name="Delfi Diagnostics, Inc.", status=status)
    with pytest.raises(CompanyValidationError, match=message) as err:
        await add_company(
            db_session, user_id=pi.id, created_by_user_id=manager.id,
            **{**_GOOD, "company_name": "DELFI Diagnostics"},
        )
    assert "Delfi Diagnostics, Inc." in str(err.value)
    assert len(await list_companies(db_session, pi.id)) == 1


async def test_the_same_name_is_fine_for_another_pi(db_session):
    pi, _agent, manager = await _pi_agent_manager(db_session)
    other = await factories.make_user(db_session)
    await seed_company(db_session, other, company_name="Belay Diagnostics")
    row = await add_company(db_session, user_id=pi.id, created_by_user_id=manager.id, **_GOOD)
    assert row.status == "confirmed"


async def test_list_companies_returns_every_status_in_name_order(db_session):
    pi, _agent, _manager = await _pi_agent_manager(db_session)
    c = await seed_company(db_session, pi, company_name="Cobalt Bio", status="rejected")
    a = await seed_company(db_session, pi, company_name="Acme Bio", status="confirmed")
    b = await seed_company(db_session, pi, company_name="Belay Diagnostics")
    assert [r.id for r in await list_companies(db_session, pi.id)] == [a.id, b.id, c.id]


async def test_confirmed_companies_for_agent_resolves_the_subject_pi(db_session):
    """Review Focus #4: an agent id with no linked user yields [] (no companies block)."""
    pi, agent, _manager = await _pi_agent_manager(db_session)
    zed = await seed_company(db_session, pi, company_name="Zed Bio", status="confirmed")
    acme = await seed_company(db_session, pi, company_name="Acme Bio", status="confirmed")
    await seed_company(db_session, pi, company_name="Belay Diagnostics")             # suggested
    await seed_company(db_session, pi, company_name="Cobalt Bio", status="rejected")
    hub = await factories.make_agent(db_session, agent_id=f"hub{uuid.uuid4().hex[:8]}", role="scout_hub")

    assert [r.id for r in await confirmed_companies_for_agent(db_session, agent.agent_id)] == [
        acme.id, zed.id]
    assert await confirmed_companies_for_agent(db_session, None) == []
    assert await confirmed_companies_for_agent(db_session, "") == []
    assert await confirmed_companies_for_agent(db_session, "nosuchagent") == []
    assert await confirmed_companies_for_agent(db_session, hub.agent_id) == []


async def test_confirm_applies_the_corrections_and_drops_the_form_d_claim(db_session):
    pi, _agent, manager = await _pi_agent_manager(db_session)
    row = await seed_company(db_session, pi)
    out = await confirm_company(
        db_session, user_id=pi.id, company_id=row.id, reviewer_id=manager.id,
        pi_role="co_founder", funding_usd=250_000_000, funding_as_of=date(2025, 6, 30),
    )
    assert out is row
    assert (row.status, row.pi_role, row.funding_usd, row.funding_as_of) == (
        "confirmed", "co_founder", 250_000_000, date(2025, 6, 30))
    assert row.funding_source_url is None       # the figure is the manager's now
    assert row.evidence["form_d"]               # the filings stay as evidence
    assert row.reviewed_by_user_id == manager.id and row.reviewed_at is not None


async def test_confirm_with_the_stored_values_keeps_the_form_d_floor(db_session):
    pi, _agent, manager = await _pi_agent_manager(db_session)
    row = await seed_company(db_session, pi)
    await confirm_company(
        db_session, user_id=pi.id, company_id=row.id, reviewer_id=manager.id,
        pi_role="founder", funding_usd=224_999_876, funding_as_of=date(2022, 3, 15),
    )
    assert (row.status, row.funding_source_url) == ("confirmed", EDGAR_URL)


async def test_confirm_with_nothing_given_keeps_every_stored_value(db_session):
    pi, _agent, manager = await _pi_agent_manager(db_session)
    row = await seed_company(db_session, pi, pi_role="board")
    await confirm_company(db_session, user_id=pi.id, company_id=row.id, reviewer_id=manager.id)
    assert (row.status, row.pi_role, row.funding_usd, row.funding_source_url) == (
        "confirmed", "board", 224_999_876, EDGAR_URL)


async def test_confirm_refuses_a_bad_correction_and_changes_nothing(db_session):
    pi, _agent, manager = await _pi_agent_manager(db_session)
    row = await seed_company(db_session, pi, funding_usd=None, funding_as_of=None,
                             funding_source_url=None)
    with pytest.raises(CompanyValidationError, match="Leave the as-of date blank"):
        await confirm_company(db_session, user_id=pi.id, company_id=row.id,
                              reviewer_id=manager.id, funding_as_of=date(2024, 1, 2))
    with pytest.raises(CompanyValidationError, match="Choose the PI"):
        await confirm_company(db_session, user_id=pi.id, company_id=row.id,
                              reviewer_id=manager.id, pi_role="ceo")
    assert row.status == "suggested"


@pytest.mark.parametrize("status", ["confirmed", "rejected"])
async def test_confirm_and_reject_act_only_on_a_suggestion(db_session, status):
    pi, _agent, manager = await _pi_agent_manager(db_session)
    row = await seed_company(db_session, pi, status=status)
    with pytest.raises(CompanyValidationError, match=f"not waiting for review \\(it is {status}\\)"):
        await confirm_company(db_session, user_id=pi.id, company_id=row.id, reviewer_id=manager.id)
    with pytest.raises(CompanyValidationError, match="not waiting for review"):
        await reject_company(db_session, user_id=pi.id, company_id=row.id, reviewer_id=manager.id)
    assert row.status == status


async def test_reject_keeps_the_row_so_the_name_stays_taken(db_session):
    pi, _agent, manager = await _pi_agent_manager(db_session)
    row = await seed_company(db_session, pi)
    out = await reject_company(db_session, user_id=pi.id, company_id=row.id, reviewer_id=manager.id)
    assert out is row
    assert (row.status, row.reviewed_by_user_id) == ("rejected", manager.id)
    assert [r.id for r in await list_companies(db_session, pi.id)] == [row.id]


async def test_delete_removes_a_confirmed_row_and_refuses_the_others(db_session):
    pi, _agent, manager = await _pi_agent_manager(db_session)
    kept = await add_company(db_session, user_id=pi.id, created_by_user_id=manager.id, **_GOOD)
    gone = await add_company(db_session, user_id=pi.id, created_by_user_id=manager.id,
                             **{**_GOOD, "company_name": "Acme Bio"})
    suggested = await seed_company(db_session, pi)
    await delete_company(db_session, user_id=pi.id, company_id=gone.id)
    with pytest.raises(CompanyValidationError, match="reject a suggestion instead"):
        await delete_company(db_session, user_id=pi.id, company_id=suggested.id)
    assert [r.id for r in await list_companies(db_session, pi.id)] == [kept.id, suggested.id]


async def test_another_pis_row_is_not_found(db_session):
    pi, _agent, manager = await _pi_agent_manager(db_session)
    other = await factories.make_user(db_session)
    row = await seed_company(db_session, other)
    for call in (
        delete_company(db_session, user_id=pi.id, company_id=row.id),
        confirm_company(db_session, user_id=pi.id, company_id=row.id, reviewer_id=manager.id),
        reject_company(db_session, user_id=pi.id, company_id=row.id, reviewer_id=manager.id),
        delete_company(db_session, user_id=pi.id, company_id=uuid.uuid4()),
    ):
        with pytest.raises(CompanyNotFoundError):
            await call
    assert row.status == "suggested"
