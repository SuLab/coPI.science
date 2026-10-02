"""The Companies card on /manager/pis/{id} and its five routes (spec 2026-10-02 §7.2): add,
delete, confirm, reject and discover, each staff-only, each flashing on a refused write and
returning to the card. Review Focus #5: two Find companies presses, or a press behind a
pending bulk job, leave exactly one job row, at interactive priority."""

import re
import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select

from src.models import (
    USER_ROLE_ADMIN,
    USER_ROLE_MANAGER,
    USER_ROLE_PI,
    USER_ROLE_REVIEWER,
    Job,
    PiCompany,
)
from src.models.job import BULK_PRIORITY, INTERACTIVE_PRIORITY
from src.services.company_discovery import DISCOVERY_DONE_STEP
from tests import factories
from tests.flash_support import session_flashes
from tests.integration._webui_helpers import impersonation_headers
from tests.integration.test_manager_access import auth_headers
from tests.integration.test_rendered_page_gate import page_problems
from tests.pi_company_support import (
    DISCOVERED_EVIDENCE,
    EDGAR_URL,
    WIKIDATA_URL,
    companies_dir,
    seed_company,
)

pytestmark = pytest.mark.integration

_ADD = {
    "company_name": "Belay Diagnostics", "pi_role": "co_founder", "funding_usd": "5,000,000",
    "funding_as_of": "2024-01-02", "source_url": "https://example.org/belay",
}
_DISABLED_FIND = re.compile(r'id="find-companies"[^>]*\sdisabled(?=[\s>])')


@pytest.fixture(autouse=True)
def _companies_dir(monkeypatch, tmp_path):
    """Every write route re-exports: never into the live profiles/ tree."""
    return companies_dir(monkeypatch, tmp_path)


async def _pi_agent_manager(db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI, name="Victor Velculescu")
    agent = await factories.make_agent(db_session, user=pi)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    return pi, agent, manager


def _back_to_the_card(r, pi) -> None:
    assert r.status_code == 302, r.status_code
    assert r.headers["location"] == f"/manager/pis/{pi.id}#companies"


def _card(body: str) -> str:
    start = body.index('<section id="companies"')
    return body[start: body.index("</section>", start)]


async def _discovery_jobs(db_session, pi) -> list[Job]:
    return list((await db_session.execute(
        select(Job).where(Job.user_id == pi.id, Job.type == "company_discovery")
    )).scalars())


async def test_add_confirms_at_once_and_writes_the_hub_file(client, db_session, _companies_dir):
    pi, agent, manager = await _pi_agent_manager(db_session)
    r = await client.post(f"/manager/pis/{pi.id}/companies", data=_ADD,
                          headers=auth_headers(manager.id), follow_redirects=False)
    _back_to_the_card(r, pi)
    row = (await db_session.execute(select(PiCompany).where(PiCompany.user_id == pi.id))).scalar_one()
    assert (row.status, row.origin, row.pi_role, row.funding_usd, row.funding_as_of) == (
        "confirmed", "manual", "co_founder", 5_000_000, date(2024, 1, 2))
    assert row.created_by_user_id == manager.id
    assert "- Belay Diagnostics: co-founder" in (_companies_dir / f"{agent.agent_id}.md").read_text()
    assert session_flashes(r)[-1] == {"text": "Added Belay Diagnostics.", "kind": "success"}


@pytest.mark.parametrize("field,value,message", [
    ("source_url", "javascript:alert(1)", "http:// or https://"),
    ("funding_usd", "12abc", "whole number of US dollars"),
    ("funding_as_of", "", "as of"),
    ("funding_as_of", "01/02/2024", "YYYY-MM-DD"),
    ("pi_role", "ceo", "Choose the PI"),
    ("company_name", "", "Enter the company"),
])
async def test_a_refused_add_flashes_and_writes_nothing(
    client, db_session, _companies_dir, field, value, message
):
    pi, agent, manager = await _pi_agent_manager(db_session)
    r = await client.post(f"/manager/pis/{pi.id}/companies", data={**_ADD, field: value},
                          headers=auth_headers(manager.id), follow_redirects=False)
    _back_to_the_card(r, pi)
    flash = session_flashes(r)[-1]
    assert flash["kind"] == "error" and message in flash["text"], flash
    assert flash["text"].startswith("Company not added: ")
    count = await db_session.scalar(
        select(func.count()).select_from(PiCompany).where(PiCompany.user_id == pi.id))
    assert count == 0
    assert not (_companies_dir / f"{agent.agent_id}.md").exists()


async def test_a_name_already_suggested_is_refused_by_name(client, db_session):
    pi, _agent, manager = await _pi_agent_manager(db_session)
    await seed_company(db_session, pi, company_name="Delfi Diagnostics, Inc.")
    r = await client.post(f"/manager/pis/{pi.id}/companies",
                          data={**_ADD, "company_name": "DELFI Diagnostics"},
                          headers=auth_headers(manager.id), follow_redirects=False)
    _back_to_the_card(r, pi)
    assert "Delfi Diagnostics, Inc. is already suggested" in session_flashes(r)[-1]["text"]


async def test_delete_asks_first_then_removes_the_row_and_the_file(client, db_session, _companies_dir):
    pi, agent, manager = await _pi_agent_manager(db_session)
    await client.post(f"/manager/pis/{pi.id}/companies", data=_ADD,
                      headers=auth_headers(manager.id), follow_redirects=False)
    row = (await db_session.execute(select(PiCompany).where(PiCompany.user_id == pi.id))).scalar_one()
    page = (await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(manager.id))).text
    assert re.search(
        rf'action="/manager/pis/{pi.id}/companies/{row.id}/delete"\s+data-confirm="Remove ', page)

    r = await client.post(f"/manager/pis/{pi.id}/companies/{row.id}/delete",
                          headers=auth_headers(manager.id), follow_redirects=False)
    _back_to_the_card(r, pi)
    assert await db_session.get(PiCompany, row.id) is None
    assert not (_companies_dir / f"{agent.agent_id}.md").exists()


async def test_confirm_takes_the_corrections_from_the_form(client, db_session, _companies_dir):
    pi, agent, manager = await _pi_agent_manager(db_session)
    row = await seed_company(db_session, pi)
    r = await client.post(
        f"/manager/pis/{pi.id}/companies/{row.id}/confirm",
        data={"pi_role": "co_founder", "funding_usd": "250000000", "funding_as_of": "2025-06-30"},
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    _back_to_the_card(r, pi)
    assert (row.status, row.pi_role, row.funding_usd, row.funding_source_url) == (
        "confirmed", "co_founder", 250_000_000, None)
    assert row.reviewed_by_user_id == manager.id
    text = (_companies_dir / f"{agent.agent_id}.md").read_text()
    assert "$250,000,000 raised as of 2025-06-30" in text


async def test_confirm_with_the_prefilled_values_keeps_the_form_d_floor(client, db_session, _companies_dir):
    pi, agent, manager = await _pi_agent_manager(db_session)
    row = await seed_company(db_session, pi)
    r = await client.post(
        f"/manager/pis/{pi.id}/companies/{row.id}/confirm",
        data={"pi_role": "founder", "funding_usd": "224999876", "funding_as_of": "2022-03-15"},
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    _back_to_the_card(r, pi)
    assert (row.status, row.funding_source_url) == ("confirmed", EDGAR_URL)
    assert ("at least $224,999,876 raised (SEC Form D filings, latest 2022-03-15)"
            in (_companies_dir / f"{agent.agent_id}.md").read_text())


async def test_a_replayed_confirm_flashes_and_changes_nothing(client, db_session):
    pi, _agent, manager = await _pi_agent_manager(db_session)
    row = await seed_company(db_session, pi, status="confirmed")
    r = await client.post(f"/manager/pis/{pi.id}/companies/{row.id}/confirm",
                          headers=auth_headers(manager.id), follow_redirects=False)
    _back_to_the_card(r, pi)
    flash = session_flashes(r)[-1]
    assert flash["kind"] == "error" and "not waiting for review" in flash["text"]


async def test_reject_keeps_the_row_unlisted_and_out_of_the_file(client, db_session, _companies_dir):
    pi, agent, manager = await _pi_agent_manager(db_session)
    row = await seed_company(db_session, pi)
    r = await client.post(f"/manager/pis/{pi.id}/companies/{row.id}/reject",
                          headers=auth_headers(manager.id), follow_redirects=False)
    _back_to_the_card(r, pi)
    assert (row.status, row.reviewed_by_user_id) == ("rejected", manager.id)
    page = (await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(manager.id))).text
    assert "DELFI Diagnostics" not in page
    assert not (_companies_dir / f"{agent.agent_id}.md").exists()


async def test_another_pis_company_and_an_unknown_one_are_404(client, db_session):
    pi, _agent, manager = await _pi_agent_manager(db_session)
    other = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    row = await seed_company(db_session, other)
    for company_id in (row.id, uuid.uuid4()):
        for action in ("delete", "confirm", "reject"):
            r = await client.post(f"/manager/pis/{pi.id}/companies/{company_id}/{action}",
                                  headers=auth_headers(manager.id), follow_redirects=False)
            assert r.status_code == 404, (company_id, action)
    assert row.status == "suggested"


async def test_a_staff_account_is_not_a_pi_target(client, db_session):
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    for path, data in ((f"/manager/pis/{admin.id}/companies", _ADD),
                       (f"/manager/pis/{admin.id}/companies/discover", None)):
        r = await client.post(path, data=data, headers=auth_headers(manager.id), follow_redirects=False)
        assert r.status_code == 404, path


async def test_a_reviewer_reaches_none_of_the_five_routes(client, db_session):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI)
    row = await seed_company(db_session, pi)
    for path in (f"/manager/pis/{pi.id}/companies", f"/manager/pis/{pi.id}/companies/discover",
                 *(f"/manager/pis/{pi.id}/companies/{row.id}/{a}" for a in ("delete", "confirm", "reject"))):
        r = await client.post(path, data=_ADD, headers=auth_headers(reviewer.id), follow_redirects=False)
        assert r.status_code == 403, path
    assert row.status == "suggested"
    assert await _discovery_jobs(db_session, pi) == []


async def test_an_admin_wearing_a_manager_adds_as_that_manager(client, db_session):
    admin = await factories.make_user(db_session, user_role=USER_ROLE_ADMIN)
    pi, _agent, manager = await _pi_agent_manager(db_session)
    r = await client.post(f"/manager/pis/{pi.id}/companies", data=_ADD,
                          headers=impersonation_headers(admin.id, manager.id), follow_redirects=False)
    _back_to_the_card(r, pi)
    row = (await db_session.execute(select(PiCompany).where(PiCompany.user_id == pi.id))).scalar_one()
    assert row.created_by_user_id == manager.id == row.reviewed_by_user_id


async def test_two_find_companies_presses_leave_one_interactive_job(client, db_session):
    """Review Focus #5."""
    pi, _agent, manager = await _pi_agent_manager(db_session)
    flashes = []
    for _ in range(2):
        r = await client.post(f"/manager/pis/{pi.id}/companies/discover",
                              headers=auth_headers(manager.id), follow_redirects=False)
        _back_to_the_card(r, pi)
        flashes.append(session_flashes(r)[-1])
    jobs = await _discovery_jobs(db_session, pi)
    assert len(jobs) == 1 and jobs[0].priority == INTERACTIVE_PRIORITY
    assert flashes[0]["kind"] == "success" and "queued" in flashes[0]["text"]
    assert flashes[1] == {
        "text": "Company discovery is already queued or running for this PI.", "kind": "info"}


async def test_a_press_behind_a_pending_bulk_job_raises_it_to_interactive(client, db_session):
    """Review Focus #5: the bulk script queued it; a manager is now waiting on it."""
    pi, _agent, manager = await _pi_agent_manager(db_session)
    bulk = Job(type="company_discovery", user_id=pi.id, status="pending",
               priority=BULK_PRIORITY, payload={"user_id": str(pi.id)})
    db_session.add(bulk)
    await db_session.flush()
    r = await client.post(f"/manager/pis/{pi.id}/companies/discover",
                          headers=auth_headers(manager.id), follow_redirects=False)
    _back_to_the_card(r, pi)
    jobs = await _discovery_jobs(db_session, pi)
    assert [j.id for j in jobs] == [bulk.id]
    await db_session.refresh(bulk)
    assert bulk.priority == INTERACTIVE_PRIORITY


async def test_the_card_shows_both_lists_with_their_evidence_and_the_last_run(client, db_session):
    pi, _agent, manager = await _pi_agent_manager(db_session)
    await seed_company(
        db_session, pi, company_name="Belay Diagnostics", status="confirmed", origin="manual",
        funding_usd=5_000_000, funding_as_of=date(2024, 1, 2), funding_source_url=None,
        source_url="https://example.org/belay", evidence=None,
        reviewed_by_user_id=manager.id, reviewed_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    delfi = await seed_company(db_session, pi)
    example = await seed_company(
        db_session, pi, company_name="Example Bio", funding_usd=None, funding_as_of=None,
        funding_source_url=None, source_url=WIKIDATA_URL,
        evidence={
            "coi": [{"pmid": "10000001", "year": 2015, "former": True, "pi_role": "founder",
                     "company_name": "Example Bio", "url": "javascript:alert(1)",
                     "sentence": "A.B.C. previously held founder equity in Example Bio."}],
            "wikidata": [{"item": "Q4115189", "url": WIKIDATA_URL, "label": "Wikidata Sandbox"}],
            "form_d": {"status": "unavailable", "funding_usd": None, "funding_as_of": None,
                       "filings_page": None, "not_fetched": 0, "filings": [],
                       "note": "funding lookup unavailable", "reason": "SEC_USER_AGENT is not set"},
            "former": True,
        },
    )
    await seed_company(db_session, pi, company_name="Egret Therapeutics", status="rejected")
    db_session.add(Job(
        type="company_discovery", user_id=pi.id, status="completed",
        completed_at=datetime(2026, 10, 2, 9, 30, tzinfo=UTC),
        payload={"user_id": str(pi.id), "progress": [
            {"step": DISCOVERY_DONE_STEP, "detail": "2 suggested, funding lookup unavailable"}]},
    ))
    await db_session.flush()

    body = (await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(manager.id))).text
    card = _card(body)
    assert "Belay Diagnostics" in card and "$5,000,000 raised as of 2024-01-02" in card
    assert "Egret Therapeutics" not in body
    assert "at least $224,999,876 raised (SEC Form D filings, latest 2022-03-15)" in card
    assert DISCOVERED_EVIDENCE["coi"][0]["sentence"] in card
    assert 'href="https://pubmed.ncbi.nlm.nih.gov/39433569/"' in card
    assert "Executive Officer, Director, Promoter" in card
    assert f'href="{WIKIDATA_URL}"' in card and "Wikidata Sandbox" in card
    assert "may be former" in card
    assert "funding lookup unavailable" in card
    # A non-http(s) link is never rendered; the PMID still links to PubMed.
    assert "javascript:" not in body
    assert 'href="https://pubmed.ncbi.nlm.nih.gov/10000001/"' in card
    for row in (delfi, example):
        assert f'action="/manager/pis/{pi.id}/companies/{row.id}/confirm"' in card
        assert f'action="/manager/pis/{pi.id}/companies/{row.id}/reject"' in card
    assert f'action="/manager/pis/{pi.id}/companies"' in card
    assert "Last run 2026-10-02 09:30 UTC: 2 suggested, funding lookup unavailable." in card
    assert not _DISABLED_FIND.search(card)


async def test_find_companies_is_disabled_while_a_job_is_pending(client, db_session):
    pi, _agent, manager = await _pi_agent_manager(db_session)
    db_session.add(Job(type="company_discovery", user_id=pi.id, status="pending",
                       payload={"user_id": str(pi.id)}))
    await db_session.flush()
    card = _card((await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(manager.id))).text)
    assert _DISABLED_FIND.search(card)
    assert "Discovery is queued" in card


async def test_a_failed_discovery_run_shows_its_error_and_can_run_again(client, db_session):
    pi, _agent, manager = await _pi_agent_manager(db_session)
    db_session.add(Job(type="company_discovery", user_id=pi.id, status="dead",
                       last_error="NCBI answered 500", payload={"user_id": str(pi.id)}))
    await db_session.flush()
    card = _card((await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(manager.id))).text)
    assert "failed (NCBI answered 500)" in card
    assert not _DISABLED_FIND.search(card)


async def test_a_reviewer_sees_the_confirmed_list_only(client, db_session):
    reviewer = await factories.make_user(db_session, user_role=USER_ROLE_REVIEWER)
    pi, _agent, _manager = await _pi_agent_manager(db_session)
    await seed_company(db_session, pi, company_name="Belay Diagnostics", status="confirmed")
    await seed_company(db_session, pi, company_name="Acme Bio")
    r = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(reviewer.id))
    assert r.status_code == 200
    card = _card(r.text)
    assert "Belay Diagnostics" in card
    assert "Acme Bio" not in r.text
    assert f"/manager/pis/{pi.id}/companies" not in r.text
    assert "find-companies" not in r.text


@pytest.mark.parametrize("role", [USER_ROLE_REVIEWER, USER_ROLE_MANAGER, USER_ROLE_ADMIN])
async def test_the_card_passes_the_rendered_page_gate(client, db_session, role):
    """Labels, headings, contrast and no inline handlers (test_rendered_page_gate.py),
    with both lists and the discovery state populated."""
    viewer = await factories.make_user(db_session, user_role=role)
    pi, _agent, manager = await _pi_agent_manager(db_session)
    await seed_company(db_session, pi, company_name="Belay Diagnostics", status="confirmed",
                       reviewed_by_user_id=manager.id, reviewed_at=datetime(2026, 10, 1, tzinfo=UTC))
    await seed_company(db_session, pi)
    db_session.add(Job(type="company_discovery", user_id=pi.id, status="completed",
                       completed_at=datetime(2026, 10, 2, tzinfo=UTC),
                       payload={"progress": [{"step": DISCOVERY_DONE_STEP, "detail": "1 suggested"}]}))
    await db_session.flush()
    r = await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(viewer.id))
    assert r.status_code == 200
    assert page_problems(r.text) == []


async def test_confirm_with_clear_funding_drops_the_prefilled_figure(client, db_session, _companies_dir):
    """The form posts the stored Form D figure prefilled; the checkbox clears it anyway."""
    pi, agent, manager = await _pi_agent_manager(db_session)
    row = await seed_company(db_session, pi)
    page = (await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(manager.id))).text
    assert f'name="clear_funding" value="1" id="company-clear-funding-{row.id}"' in _card(page)
    r = await client.post(
        f"/manager/pis/{pi.id}/companies/{row.id}/confirm",
        data={"pi_role": "founder", "funding_usd": "224999876", "funding_as_of": "2022-03-15",
              "clear_funding": "1"},
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    _back_to_the_card(r, pi)
    assert (row.status, row.funding_usd, row.funding_as_of, row.funding_source_url) == (
        "confirmed", None, None, None)
    text = (_companies_dir / f"{agent.agent_id}.md").read_text()
    assert "- DELFI Diagnostics: founder" in text and "Funding" not in text.split("- DELFI")[1]


async def test_clear_funding_ignores_an_unparseable_figure(client, db_session):
    pi, _agent, manager = await _pi_agent_manager(db_session)
    row = await seed_company(db_session, pi)
    r = await client.post(
        f"/manager/pis/{pi.id}/companies/{row.id}/confirm",
        data={"funding_usd": "12abc", "clear_funding": "1"},
        headers=auth_headers(manager.id), follow_redirects=False,
    )
    _back_to_the_card(r, pi)
    assert (row.status, row.funding_usd) == ("confirmed", None)


async def test_find_companies_for_a_pi_without_an_orcid_says_so_and_queues_nothing(client, db_session):
    pi = await factories.make_user(db_session, user_role=USER_ROLE_PI, orcid="")
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    r = await client.post(f"/manager/pis/{pi.id}/companies/discover",
                          headers=auth_headers(manager.id), follow_redirects=False)
    _back_to_the_card(r, pi)
    assert session_flashes(r)[-1] == {
        "text": "Company discovery needs the PI's ORCID iD; none is recorded.", "kind": "error"}
    assert await _discovery_jobs(db_session, pi) == []


async def test_the_card_shows_the_note_of_an_ambiguous_funding_lookup(client, db_session):
    pi, _agent, manager = await _pi_agent_manager(db_session)
    note = "Two issuers file Form D under this name; no figure recorded."
    await seed_company(
        db_session, pi, funding_usd=None, funding_as_of=None, funding_source_url=None,
        evidence={**DISCOVERED_EVIDENCE, "form_d": {
            "status": "ambiguous", "funding_usd": None, "filings": [], "note": note}},
    )
    card = _card((await client.get(f"/manager/pis/{pi.id}", headers=auth_headers(manager.id))).text)
    assert note in card
    assert "clear_funding" not in card      # no figure, nothing to clear
