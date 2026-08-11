"""_load_publication_records: publications table → per-agent DOI ground truth."""

import uuid

import pytest

from src.agent.agent import Agent
from src.agent.simulation import SimulationEngine
from src.models import AgentRegistry, Publication, User

pytestmark = pytest.mark.integration


async def _seed(db, agent_id: str, dois: list[str | None]) -> None:
    user = User(id=uuid.uuid4(), name=f"PI {agent_id}", orcid=f"0000-{agent_id}")
    db.add(user)
    await db.flush()
    db.add(
        AgentRegistry(
            agent_id=agent_id,
            bot_name=f"{agent_id.title()}Bot",
            pi_name=f"PI {agent_id}",
            status="active",
            user_id=user.id,
        )
    )
    for doi in dois:
        db.add(Publication(user_id=user.id, doi=doi, title=f"Paper {doi}"))
    await db.flush()


async def test_load_publication_records(db_session):
    await _seed(db_session, "wu", ["10.1093/bioadv/vbag036", None])
    await _seed(db_session, "good", [])

    wu = Agent(agent_id="wu", bot_name="WuBot", pi_name="Chunlei Wu")
    good = Agent(agent_id="good", bot_name="GoodBot", pi_name="Benjamin Good")
    engine = SimulationEngine(agents=[wu, good], slack_clients={})

    await engine._load_publication_records(db_session)

    # Wu: one DOI row + one NULL-DOI row → has_records, normalized DOI set.
    assert engine._agent_publications["wu"].has_records is True
    assert engine._agent_publications["wu"].dois == {"10.1093/bioadv/vbag036"}
    # Good: registry row but zero publications → absent (== no records).
    assert "good" not in engine._agent_publications
    # DB DOIs are pushed onto the Agent for the intake guard too.
    assert "10.1093/bioadv/vbag036" in wu.own_publication_dois
    assert good.own_publication_dois == set()
