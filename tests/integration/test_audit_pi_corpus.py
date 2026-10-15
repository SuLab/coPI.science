"""scripts/audit_pi_corpus.py --baseline reads the database read-only (spec 2026-10-15
R3.10): SET TRANSACTION READ ONLY first, every read before any network call, no write."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scripts import audit_pi_corpus as audit
from src.models.publication import Publication
from tests.factories import make_user

_ORCID = "0000-0002-1825-0097"


async def _snapshot(db_session, user_id):
    return (await db_session.execute(
        select(Publication.id, Publication.provenance, Publication.excluded_at,
               Publication.excluded_by_user_id)
        .where(Publication.user_id == user_id).order_by(Publication.title)
    )).all()


async def test_baseline_session_is_read_only(db_session, monkeypatch):
    user = await make_user(db_session, orcid=_ORCID, name="Rachel Green",
                           institution="Johns Hopkins University")
    db_session.add_all([
        Publication(user_id=user.id, pmid="101", title="Anchored", provenance="s1,s3"),
        Publication(user_id=user.id, pmid=None, doi="10.1/oa", title="OpenAlex only",
                    provenance="openalex"),
        Publication(user_id=user.id, pmid="102", title="Excluded", provenance="s1",
                    excluded_at=datetime(2026, 10, 1, tzinfo=UTC)),
    ])
    await db_session.flush()
    before = await _snapshot(db_session, user.id)

    reader = AsyncSession(bind=await db_session.connection(), expire_on_commit=False,
                          join_transaction_mode="create_savepoint")
    statements: list[str] = []
    real_execute = reader.execute

    async def spy(statement, *args, **kwargs):
        statements.append(str(statement))
        return await real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(reader, "execute", spy)
    try:
        pis = await audit.read_baseline_rows(reader, [_ORCID])
    finally:
        await reader.close()
    assert statements[0] == "SET TRANSACTION READ ONLY"

    fetched: list[list[str]] = []

    async def fake_fetch(pmids, **kwargs):
        fetched.append(list(pmids))
        return [{"pmid": "101", "title": "Anchored", "year": 2020, "abstract": "A.",
                 "pub_types": ["Journal Article"],
                 "authors": [{"last": "Green", "fore": "Rachel", "initials": "R",
                              "collective": None, "affiliations": []}]}]

    monkeypatch.setattr(audit, "fetch_pubmed_records", fake_fetch)
    (pi,) = pis
    line = await audit.baseline_line(pi, skip_resolve=True)

    assert fetched == [["101"]]
    assert line["in_use"] == 2
    assert line["rows"] == [{"pmid": "101", "provenance": "s1,s3", "verdict": "kept"}]
    assert line["stored_pmids"] == ["101", "102"]
    assert line["gate_inputs"] is None and line["openalex"] is None
    assert "abstract" not in line["row_records"][0]
    assert await _snapshot(db_session, user.id) == before
