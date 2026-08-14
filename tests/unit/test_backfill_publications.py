"""Curated publications backfill for labs whose ORCID works are empty.

Eleven active labs have zero publications rows because the only ingest path
(profile pipeline: ORCID works -> PMID) found nothing for them, which mutes
them under the issue-29 fail-closed authorship guard. The backfill takes a
curated agent_id -> PMID mapping, fetches the PubMed records, and inserts
Publication rows; the running simulation picks them up on the next ~30s
roster sync with no restart.
"""

import uuid

import pytest
from sqlalchemy import func, select

from scripts.backfill_publications import backfill
from src.models import AgentRegistry, Publication, User

pytestmark = pytest.mark.integration


async def _seed_agent(db, agent_id: str) -> User:
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
    await db.flush()
    return user


def _fake_fetch(records: list[dict]):
    """Async fetch stub that records which PMIDs were requested."""
    requested: list[list[str]] = []

    async def fetch(pmids: list[str]) -> list[dict]:
        requested.append(list(pmids))
        return [r for r in records if r["pmid"] in pmids]

    fetch.requested = requested  # type: ignore[attr-defined]
    return fetch


async def _count_pubs(db, user_id) -> int:
    return (
        await db.execute(
            select(func.count()).select_from(Publication).where(
                Publication.user_id == user_id
            )
        )
    ).scalar()


REC = {
    "pmid": "111",
    "title": "A real paper",
    "abstract": "An abstract.",
    "journal": "J. Test",
    "year": 2024,
    "doi": "doi:10.1000/xyz.1",  # prefixed on purpose: must land canonicalized
}


async def test_dry_run_reports_but_writes_nothing(db_session):
    user = await _seed_agent(db_session, "good")
    fetch = _fake_fetch([REC])

    report = await backfill(db_session, {"good": ["111"]}, fetch=fetch, apply=False)

    assert ("good", "would-insert", "111") in report
    assert await _count_pubs(db_session, user.id) == 0


async def test_apply_inserts_row_with_canonical_doi(db_session):
    user = await _seed_agent(db_session, "good")
    fetch = _fake_fetch([REC])

    report = await backfill(db_session, {"good": ["111"]}, fetch=fetch, apply=True)

    assert ("good", "insert", "111") in report
    row = (
        await db_session.execute(
            select(Publication).where(Publication.user_id == user.id)
        )
    ).scalar_one()
    assert row.pmid == "111"
    assert row.doi == "10.1000/xyz.1"
    assert row.title == "A real paper"


async def test_pmids_the_user_already_has_are_skipped_and_not_fetched(db_session):
    user = await _seed_agent(db_session, "good")
    db_session.add(Publication(user_id=user.id, pmid="111", title="Already here"))
    await db_session.flush()
    fetch = _fake_fetch([REC])

    report = await backfill(db_session, {"good": ["111"]}, fetch=fetch, apply=True)

    assert ("good", "skip-existing", "111") in report
    assert await _count_pubs(db_session, user.id) == 1
    assert fetch.requested in ([], [[]])  # nothing left to fetch


async def test_unknown_agent_is_reported_not_fatal(db_session):
    fetch = _fake_fetch([])

    report = await backfill(db_session, {"ghost": ["111"]}, fetch=fetch, apply=True)

    assert ("ghost", "error-no-agent", "") in report


async def test_pmid_with_no_pubmed_record_is_reported(db_session):
    await _seed_agent(db_session, "good")
    fetch = _fake_fetch([])  # PubMed returns nothing for the pmid

    report = await backfill(db_session, {"good": ["999"]}, fetch=fetch, apply=True)

    assert ("good", "error-no-record", "999") in report
