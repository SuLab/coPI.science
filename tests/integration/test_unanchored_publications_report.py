"""scripts/unanchored_publications_report.py (spec 2026-10-05 §6.3 data repair 1-2)."""
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

from src.models import AppSetting, Job, Publication
from src.services.corpus import CorpusResult
from src.services.openalex_budget import Meter
from tests import factories

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
pytestmark = pytest.mark.integration
_HEALTHY_METER = Meter(1000, 1000, 600)


def _rec(pmid, stages, year=2020, title=None):
    return {"pmid": pmid, "title": title or f"Paper {pmid}", "year": year, "stages": stages,
            "abstract": "A.", "journal": "J"}


async def _seed(db_session):
    pi = await factories.make_user(db_session, name="Ada Lovelace")
    for pmid, prov, excluded in [("1", None, False), ("2", None, False), ("3", "manual", False),
                                 ("4", None, True), ("5", "s1", False)]:
        db_session.add(Publication(user_id=pi.id, pmid=pmid, title=f"Stored {pmid}", year=2019,
                                   provenance=prov,
                                   excluded_at=func.now() if excluded else None))
    await db_session.flush()
    result = CorpusResult(kept=[], flagged=[], ranked=[
        _rec("1", ["s1"], year=2021), _rec("2", ["s4"]), _rec("9", ["s3"]),
    ])
    return pi, result


def _wire(monkeypatch, rep, result, meter=_HEALTHY_METER):
    async def fake_resolve(orcid, name, institution, **kw):
        return result

    async def fake_meter():
        return meter

    monkeypatch.setattr(rep, "resolve_corpus", fake_resolve)
    monkeypatch.setattr(rep, "read_meter", fake_meter)


async def test_compare_lists_every_category(db_session):
    import scripts.unanchored_publications_report as rep

    pi, result = await _seed(db_session)
    rows = (await db_session.execute(
        select(Publication).where(Publication.user_id == pi.id))).scalars().all()
    report = rep.compare(rows, result)
    assert [p for p, _y, _t in report.unstored_anchored] == ["9"]
    assert ("1", 2019, 2021) in report.year_changes
    assert sorted(pmid for _i, pmid, _t in report.unanchored) == ["2", "5"]


async def test_apply_marks_only_the_unanchored_set_and_enqueues_nothing(db_session, monkeypatch):
    import scripts.unanchored_publications_report as rep

    pi, result = await _seed(db_session)
    _wire(monkeypatch, rep, result)
    report = await rep.build_report(db_session, pi.id, pi.orcid, pi.name, pi.institution)
    marked = await rep.apply_report(db_session, report)
    provs = dict((await db_session.execute(select(Publication.pmid, Publication.provenance).where(
        Publication.user_id == pi.id))).all())
    assert marked == 2
    assert provs == {"1": None, "2": "unanchored", "3": "manual", "4": None, "5": "unanchored"}
    assert (await db_session.execute(select(func.count()).select_from(Job).where(
        Job.user_id == pi.id))).scalar_one() == 0


async def test_id_named_accounts_are_listed(db_session):
    import scripts.unanchored_publications_report as rep

    await factories.make_user(db_session, name="0000-0002-1825-0097", user_role="manager")
    names = [n for n, _role, _agent in await rep.id_named_accounts(db_session)]
    assert "0000-0002-1825-0097" in names


async def test_run_apply_records_the_marker(db_session, monkeypatch):
    import scripts.unanchored_publications_report as rep

    pi, result = await _seed(db_session)
    _wire(monkeypatch, rep, result)
    assert await rep.run(db_session, orcids=[pi.orcid], apply=True) == 0
    row = await db_session.get(AppSetting, rep.APPLIED_KEY)
    assert row is not None and '"rows_marked": 2' in row.value and '"failed": []' in row.value


@pytest.mark.parametrize("meter", [Meter(1000, 4, 600), None])
async def test_an_unsafe_openalex_budget_stops_before_resolving_and_records_no_marker(
    db_session, monkeypatch, capsys, meter,
):
    import scripts.unanchored_publications_report as rep

    pi, result = await _seed(db_session)
    before = await db_session.get(AppSetting, rep.APPLIED_KEY)
    before_value = before.value if before is not None else None
    # Four free credits cannot cover the five-credit admission estimate.
    _wire(monkeypatch, rep, result, meter=meter)
    assert await rep.run(db_session, orcids=[pi.orcid], apply=True) == 1
    out = capsys.readouterr().out
    assert "STOPPED before Ada Lovelace" in out and f"--orcid {pi.orcid}" in out
    provs = dict((await db_session.execute(select(Publication.pmid, Publication.provenance).where(
        Publication.user_id == pi.id))).all())
    assert provs["2"] is None and provs["5"] == "s1"
    after = await db_session.get(AppSetting, rep.APPLIED_KEY)
    assert (after.value if after is not None else None) == before_value


async def test_exhaustion_during_resolve_leaves_rows_unchanged_and_marks_the_run_failed(
    db_session, monkeypatch,
):
    import scripts.unanchored_publications_report as rep
    from src.services import openalex_budget as ob

    pi, result = await _seed(db_session)
    _wire(monkeypatch, rep, result)

    async def exhausted():
        return Meter(1000, 0, 600)

    async def resolve(*args, **kwargs):
        await ob.check_request_budget()
        raise AssertionError("A corpus request must not run after the free budget is exhausted")

    monkeypatch.setattr(ob, "read_meter", exhausted)
    monkeypatch.setattr(rep, "resolve_corpus", resolve)
    assert await rep.run(db_session, orcids=[pi.orcid], apply=True) == 1
    provs = dict((await db_session.execute(select(Publication.pmid, Publication.provenance).where(
        Publication.user_id == pi.id))).all())
    assert provs["2"] is None and provs["5"] == "s1"
    marker = await db_session.get(AppSetting, rep.APPLIED_KEY)
    assert marker is not None and pi.orcid in marker.value and '"rows_marked": 0' in marker.value
    await ob.check_request_budget()  # The script's task-local guard was restored.
