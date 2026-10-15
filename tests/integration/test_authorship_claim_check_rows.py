"""Live rows and C12 grounding through the engine (the 2026-10-15 publication-handling
design §11 R6.7, R6.10). The engine's session factory is a savepoint session on the test's
own connection, so the rows roll back with the test."""
import contextlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.agent import Agent
from src.agent.message_log import LogEntry
from src.agent.simulation import SimulationEngine
from src.config import get_settings
from src.models import AuthorshipClaimCheck, Publication
from tests import factories
from tests.fakes import FakeSlackClient

pytestmark = pytest.mark.integration
T = "1600000700.000000"


@pytest.fixture
async def on_test_connection(db_session):
    conn = await db_session.connection()

    @contextlib.asynccontextmanager
    async def _factory():
        s = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
        try:
            yield s
        finally:
            await s.close()

    return _factory


@pytest.fixture(autouse=True)
def _log_mode(monkeypatch):
    monkeypatch.setenv("AUTHORSHIP_CLAIM_CHECK_MODE", "log")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _engine(db_session, factory, tmp_path, monkeypatch, *, persona: str, rows):
    """wang (pi_lab) with ``rows`` = [(doi, excluded_reason or None)], the hub, a run, a
    thread T rooted by the hub; C12 records loaded through the engine's roster sync."""
    run = await factories.make_simulation_run(db_session)
    user = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=user, agent_id="wang", bot_name="WangBot",
                               pi_name="Jane Wang", role="pi_lab")
    for i, (doi, reason) in enumerate(rows):
        db_session.add(Publication(
            user_id=user.id, pmid=str(40_000_000 + i), doi=doi, title=f"Paper {i}", year=2020,
            excluded_at=datetime.now(UTC) if reason else None, excluded_reason=reason,
        ))
    await db_session.flush()
    monkeypatch.setattr("src.agent.agent.PROFILES_DIR", tmp_path)
    (tmp_path / "public").mkdir(exist_ok=True)
    (tmp_path / "public" / "wang.md").write_text(persona, encoding="utf-8")
    lab = Agent("wang", "WangBot", "Jane Wang", role="pi_lab")
    hub = Agent("blackbird", "BlackbirdBot", "Blackbird", role="scout_hub")
    eng = SimulationEngine(
        agents=[lab, hub],
        slack_clients={a.agent_id: FakeSlackClient(agent_id=a.agent_id) for a in (lab, hub)},
        session_factory=factory, simulation_run_id=run.id,
    )
    await eng._sync_publication_records()
    eng.message_log.append(LogEntry(
        ts=T, channel="general", sender_agent_id="blackbird", sender_name="BlackbirdBot",
        content="Which of your results would you build on?", posted_at=float(T), is_bot=True,
    ))
    return eng, run


async def _rows(db_session, run):
    return (await db_session.execute(
        select(AuthorshipClaimCheck).where(AuthorshipClaimCheck.simulation_run_id == run.id)
        .order_by(AuthorshipClaimCheck.created_at)
    )).scalars().all()


async def test_rows_are_written_with_the_run_id(db_session, on_test_connection, tmp_path, monkeypatch):
    eng, run = await _engine(db_session, on_test_connection, tmp_path, monkeypatch,
                             persona="# Jane Wang Lab\n", rows=[])
    await eng._post_message("wang", "general", "Three isogenic lines.", thread_ts=T)
    rows = await _rows(db_session, run)
    assert [(r.simulation_run_id, r.agent_id, r.phase, r.outcome) for r in rows] == [
        (run.id, "wang", "thread_reply", "no_claim")]


async def test_a_stored_paper_beyond_the_persona_grounds_a_claim(db_session, on_test_connection, tmp_path, monkeypatch):
    dois = [f"10.5555/paper.{i}" for i in range(25)]
    persona = "# Jane Wang Lab\n\n## Recent Publications\n\n" + "".join(
        f"- Paper {i}. https://doi.org/{d}\n" for i, d in enumerate(dois[:20]))
    eng, run = await _engine(db_session, on_test_connection, tmp_path, monkeypatch,
                             persona=persona, rows=[(d, None) for d in dois])
    await eng._post_message("wang", "general", f"We published https://doi.org/{dois[24]}.", thread_ts=T)
    assert [r.outcome for r in await _rows(db_session, run)] == ["grounded"]


_F9_PERSONA = ("# Jane Wang Lab\n\n## Recent Publications\n\n"
               "- Kept. https://doi.org/10.5555/kept.1\n"
               "- Excluded. https://doi.org/10.5555/excluded.1\n")


async def test_a_persona_doi_held_only_by_a_staff_excluded_row_is_not_grounded(db_session, on_test_connection, tmp_path, monkeypatch):
    eng, run = await _engine(db_session, on_test_connection, tmp_path, monkeypatch,
                             persona=_F9_PERSONA, rows=[("10.5555/excluded.1", "staff")])
    await eng._post_message("wang", "general", "We published https://doi.org/10.5555/excluded.1.", thread_ts=T)
    (row,) = await _rows(db_session, run)
    assert (row.outcome, row.reasons[0]["code"]) == ("rejected", "not_held")


async def test_a_persona_doi_held_only_by_a_superseded_row_still_grounds(db_session, on_test_connection, tmp_path, monkeypatch):
    eng, run = await _engine(db_session, on_test_connection, tmp_path, monkeypatch,
                             persona=_F9_PERSONA, rows=[("10.5555/excluded.1", "superseded")])
    await eng._post_message("wang", "general", "We published https://doi.org/10.5555/excluded.1.", thread_ts=T)
    assert [r.outcome for r in await _rows(db_session, run)] == ["grounded"]
