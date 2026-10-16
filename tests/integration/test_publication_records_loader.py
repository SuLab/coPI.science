"""C12's loader against a migrated database (spec 2026-10-15 §10 R5.4)."""
from datetime import UTC, datetime

import pytest

from src.agent.publication_records import PublicationIds, load_publication_ids
from src.models import Publication
from src.services.jhu_rules import set_tenure_start
from tests import factories

pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 1, tzinfo=UTC)


async def test_load_publication_ids_reads_in_use_and_not_theirs_rows(db_session):
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, agent_id="smith")
    await factories.make_agent(db_session, agent_id="blackbird", role="scout_hub")
    db_session.add_all([
        Publication(user_id=pi.id, pmid="1", title="In use", provenance="s1"),
        Publication(user_id=pi.id, pmid="2", title="Staff", excluded_at=NOW,
                    excluded_reason="staff"),
        Publication(user_id=pi.id, pmid="3", title="Superseded", excluded_at=NOW,
                    excluded_reason="superseded"),
        Publication(user_id=pi.id, pmid="4", title="Legacy", excluded_at=NOW),
        Publication(user_id=pi.id, pmid="5", doi="10.5555/twin", title="Staff twin",
                    excluded_at=NOW, excluded_reason="staff"),
        Publication(user_id=pi.id, pmid="6", doi="10.5555/TWIN", title="Superseded twin",
                    excluded_at=NOW, excluded_reason="superseded"),
        Publication(user_id=pi.id, pmid=None, doi="10.5555/OA.1", title="OpenAlex only",
                    provenance="unanchored"),
    ])
    await db_session.flush()
    out = await load_publication_ids(db_session, ["smith", "blackbird", "nobody"])
    smith = out["smith"]
    assert smith.in_use_pmids == {"1"}
    assert smith.in_use_dois == {"10.5555/oa.1"}
    assert smith.not_theirs_pmids == {"2", "5"}
    # The superseded row holds the twin's DOI too, so it stays the lab's own (C12).
    assert smith.not_theirs_dois == frozenset()
    assert smith.row_count == 2
    assert out["blackbird"] == PublicationIds()
    assert out["nobody"] == PublicationIds()


async def test_load_publication_ids_ignores_tenure_scope(db_session):
    pi = await factories.make_user(db_session)
    await factories.make_agent(db_session, user=pi, agent_id="tenured")
    await set_tenure_start(pi.id, 2020, "manual", db=db_session)
    db_session.add(Publication(user_id=pi.id, pmid="2005", title="Old", year=2005))
    await db_session.flush()
    out = await load_publication_ids(db_session, ["tenured"])
    assert "2005" in out["tenured"].in_use_pmids


async def test_load_publication_ids_without_ids_runs_no_query():
    executed = []

    class _NoDb:
        async def execute(self, *args, **kwargs):
            executed.append(args)

    assert await load_publication_ids(_NoDb(), []) == {}
    assert executed == []
