"""scripts/openalex_attribution_report.py (R4.7): the pipeline's rule, read-only, metered."""
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from src.models import Publication
from src.services.pubmed import _doi_fold
from tests import factories

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
pytestmark = pytest.mark.integration

HOPKINS = {"institutions": ["Johns Hopkins University"], "raw_affiliations": [],
           "orcid_in_metadata": False}
ELSEWHERE = {"institutions": ["University of Elsewhere"], "raw_affiliations": [],
             "orcid_in_metadata": False}


def test_classify_openalex_rows():
    import scripts.openalex_attribution_report as rep

    a, b, c = (rep.StoredWork(f"10.1/{x}", 2020, x) for x in "abc")
    authorships = {_doi_fold(k): v for k, v in {"10.1/A": HOPKINS, "10.1/b": ELSEWHERE}.items()}
    assert rep.classify_openalex_rows([a, b, c], authorships,
                                      ["Example Institute of Science"]) == ([b], [a], [c])


def _wire(monkeypatch, rep, budget=(None,)):
    reasons = list(budget)

    async def low():
        return reasons.pop(0) if reasons else None

    async def profile(orcid):
        return {"organizations": ["Example Institute of Science"]}

    async def authorships(orcid):
        return {"10.1/a": HOPKINS, "10.1/b": ELSEWHERE}

    monkeypatch.setattr(rep, "_budget_low", low)
    monkeypatch.setattr(rep, "fetch_orcid_profile", profile)
    monkeypatch.setattr(rep, "orcid_authorships_by_doi", authorships)


async def _pi(db_session, name):
    pi = await factories.make_user(db_session, name=name)
    db_session.add_all([
        Publication(user_id=pi.id, doi="10.1/a", title="A", year=2021, provenance="openalex"),
        Publication(user_id=pi.id, doi="10.1/b", title="B", year=2022, provenance="openalex"),
    ])
    await db_session.flush()
    return pi


async def test_the_report_writes_nothing(db_session, monkeypatch):
    import scripts.openalex_attribution_report as rep

    _wire(monkeypatch, rep)
    pi = await _pi(db_session, "Ada Alpha")

    async def snapshot():
        rows = (await db_session.execute(select(Publication).where(
            Publication.user_id == pi.id).order_by(Publication.doi)
            .execution_options(populate_existing=True))).scalars().all()
        return [{c.key: getattr(r, c.key) for c in r.__table__.columns} for r in rows]

    before = await snapshot()
    statements: list[str] = []
    real_execute = db_session.execute

    async def spy(statement, *args, **kwargs):
        statements.append(str(statement))
        return await real_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", spy)
    assert await rep.run(db_session, orcids=[pi.orcid]) == 0
    monkeypatch.setattr(db_session, "execute", real_execute)
    assert await snapshot() == before
    # The population is read inside a read-only transaction: its first statement.
    assert statements[0] == "SET TRANSACTION READ ONLY"
    assert not any(s.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))
                   for s in statements)


async def test_a_budget_stop_lists_the_rest_and_exits_1(db_session, monkeypatch, capsys):
    import scripts.openalex_attribution_report as rep

    _wire(monkeypatch, rep, budget=(None, "OpenAlex free budget: 0 of 1000 credits left"))
    first = await _pi(db_session, "Ada Alpha")
    second = await _pi(db_session, "Bea Beta")
    assert await rep.run(db_session, orcids=[first.orcid, second.orcid]) == 1
    out = capsys.readouterr().out
    assert f"--orcid {second.orcid}" in out and "would_exclude (1):" in out
