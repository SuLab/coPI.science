"""The hub's companies file (spec 2026-10-02 §7.2, §7.3): written from confirmed rows only
after every write, removed with the last one, refreshed by the profile publish, a no-op
for a PI with no agent row (Review Focus #4), and removed with the account."""

import logging
from datetime import date

import pytest
from sqlalchemy import text

from src.models import USER_ROLE_MANAGER
from src.services import pi_companies, profile_export, user_deletion
from src.services.pi_companies import (
    add_company,
    confirm_company,
    delete_company,
    export_companies_file,
    reject_company,
)
from src.services.profile_publish import export_and_record
from src.services.user_deletion import delete_user_account
from tests import factories
from tests.pi_company_support import companies_dir, seed_company

pytestmark = pytest.mark.integration

_GOOD = dict(
    company_name="Belay Diagnostics", pi_role="co_founder", funding_usd=5_000_000,
    funding_as_of=date(2024, 1, 2), source_url="https://example.org/belay",
)


@pytest.fixture(autouse=True)
def _companies_dir(monkeypatch, tmp_path):
    return companies_dir(monkeypatch, tmp_path)


async def _pi_agent_manager(db_session, **agent_overrides):
    pi = await factories.make_user(db_session, name="Victor Velculescu")
    agent = await factories.make_agent(db_session, user=pi, **agent_overrides)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    return pi, agent, manager


async def test_every_write_rewrites_the_file_from_confirmed_rows_only(db_session, _companies_dir):
    pi, agent, manager = await _pi_agent_manager(db_session)
    path = _companies_dir / f"{agent.agent_id}.md"
    suggested = await seed_company(db_session, pi)
    await seed_company(db_session, pi, company_name="Egret Therapeutics", status="rejected")
    assert await export_companies_file(db_session, pi.id) is None
    assert not path.exists(), "suggested and rejected rows never reach the hub"

    await add_company(db_session, user_id=pi.id, created_by_user_id=manager.id, **_GOOD)
    first = path.read_text()
    assert "- Belay Diagnostics: co-founder" in first
    assert "  - Funding: $5,000,000 raised as of 2024-01-02" in first
    assert "DELFI" not in first and "Egret" not in first

    await confirm_company(db_session, user_id=pi.id, company_id=suggested.id, reviewer_id=manager.id)
    second = path.read_text()
    assert "- DELFI Diagnostics: founder" in second
    assert "at least $224,999,876 raised (SEC Form D filings, latest 2022-03-15)" in second
    assert "Egret" not in second


async def test_reject_leaves_the_file_as_it_was(db_session, _companies_dir):
    pi, agent, manager = await _pi_agent_manager(db_session)
    await add_company(db_session, user_id=pi.id, created_by_user_id=manager.id, **_GOOD)
    before = (_companies_dir / f"{agent.agent_id}.md").read_text()
    row = await seed_company(db_session, pi)
    await reject_company(db_session, user_id=pi.id, company_id=row.id, reviewer_id=manager.id)
    assert (_companies_dir / f"{agent.agent_id}.md").read_text() == before


async def test_the_file_goes_with_the_last_confirmed_row(db_session, _companies_dir):
    pi, agent, manager = await _pi_agent_manager(db_session)
    row = await add_company(db_session, user_id=pi.id, created_by_user_id=manager.id, **_GOOD)
    path = _companies_dir / f"{agent.agent_id}.md"
    assert path.exists()
    await delete_company(db_session, user_id=pi.id, company_id=row.id)
    assert not path.exists()


async def test_a_pi_with_no_agent_row_gets_no_file_and_reviews_still_work(db_session, _companies_dir):
    """Review Focus #4."""
    pi = await factories.make_user(db_session)
    manager = await factories.make_user(db_session, user_role=USER_ROLE_MANAGER)
    added = await add_company(db_session, user_id=pi.id, created_by_user_id=manager.id, **_GOOD)
    confirmed = await confirm_company(
        db_session, user_id=pi.id, company_id=(await seed_company(db_session, pi)).id,
        reviewer_id=manager.id,
    )
    rejected = await reject_company(
        db_session, user_id=pi.id,
        company_id=(await seed_company(db_session, pi, company_name="Acme Bio")).id,
        reviewer_id=manager.id,
    )
    assert (added.status, confirmed.status, rejected.status) == ("confirmed", "confirmed", "rejected")
    assert await export_companies_file(db_session, pi.id) is None
    assert not _companies_dir.exists()


async def test_an_unsafe_agent_id_writes_nothing(db_session, _companies_dir, caplog):
    pi, _agent, manager = await _pi_agent_manager(db_session, agent_id="Bad.Id")
    with caplog.at_level(logging.ERROR, logger="src.services.pi_companies"):
        row = await add_company(db_session, user_id=pi.id, created_by_user_id=manager.id, **_GOOD)
    assert row.status == "confirmed"
    assert not _companies_dir.exists()
    assert "unsafe agent_id 'Bad.Id'" in caplog.text


async def test_a_filesystem_failure_is_logged_and_the_write_stands(db_session, monkeypatch, tmp_path, caplog):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a file where the directory should be")
    monkeypatch.setattr(pi_companies, "COMPANIES_DIR", blocker)
    pi, _agent, manager = await _pi_agent_manager(db_session)
    with caplog.at_level(logging.ERROR, logger="src.services.pi_companies"):
        row = await add_company(db_session, user_id=pi.id, created_by_user_id=manager.id, **_GOOD)
    assert row.status == "confirmed"
    assert "Companies export for agent" in caplog.text


async def test_the_profile_publish_writes_the_file_for_a_later_agent(db_session, monkeypatch, tmp_path, _companies_dir):
    """§7.2: rows confirmed before the agent existed reach the hub at the next publish."""
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path / "public")
    pi = await factories.make_user(db_session, name="Victor Velculescu")
    profile = await factories.make_profile(db_session, user=pi)
    await seed_company(db_session, pi, status="confirmed")
    assert await export_companies_file(db_session, pi.id) is None      # no agent yet
    agent = await factories.make_agent(db_session, user=pi)

    await export_and_record(db_session, user=pi, profile=profile, agent=agent,
                            publications=None, mechanism=None)
    assert "- DELFI Diagnostics: founder" in (_companies_dir / f"{agent.agent_id}.md").read_text()


async def test_the_profile_publish_without_an_agent_writes_no_file(db_session, monkeypatch, tmp_path, _companies_dir):
    monkeypatch.setattr(profile_export, "PROFILES_DIR", tmp_path / "public")
    pi = await factories.make_user(db_session)
    profile = await factories.make_profile(db_session, user=pi)
    await seed_company(db_session, pi, status="confirmed")
    assert await export_and_record(db_session, user=pi, profile=profile, agent=None,
                                   publications=None, mechanism="web") is None
    assert not _companies_dir.exists()


async def test_deleting_the_pi_removes_the_file_and_the_rows(db_session, monkeypatch, tmp_path, _companies_dir):
    monkeypatch.setattr(user_deletion, "_PUBLIC_DIR", tmp_path / "pub")
    monkeypatch.setattr(user_deletion, "_MEMORY_DIR", tmp_path / "mem")
    pi, agent, manager = await _pi_agent_manager(db_session)
    await add_company(db_session, user_id=pi.id, created_by_user_id=manager.id, **_GOOD)
    path = _companies_dir / f"{agent.agent_id}.md"
    assert path.exists()
    pi_id = pi.id

    report = await delete_user_account(db_session, pi)

    assert not path.exists()
    assert str(path) in report.files_deleted
    left = (await db_session.execute(
        text("SELECT count(*) FROM pi_companies WHERE user_id = :u"), {"u": pi_id}
    )).scalar_one()
    assert left == 0


async def test_deleting_a_reviewer_keeps_the_rows_and_clears_the_attribution(db_session):
    pi, _agent, manager = await _pi_agent_manager(db_session)
    row = await add_company(db_session, user_id=pi.id, created_by_user_id=manager.id, **_GOOD)
    await db_session.execute(text("DELETE FROM users WHERE id = :u"), {"u": manager.id})
    got = (await db_session.execute(
        text("SELECT created_by_user_id, reviewed_by_user_id, status FROM pi_companies WHERE id = :i"),
        {"i": row.id},
    )).one()
    assert tuple(got) == (None, None, "confirmed")
