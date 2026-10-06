"""ORCID fundings: parsing, fetch modes, storage (spec 2026-10-05 §6.1)."""
from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import select

from src.models import PiGrantIdentity, PiOrcidFunding
from src.services import http_pacing
from src.services import orcid_fundings as of
from tests import factories

BASE = "https://pub.orcid.org/v3.0"
OID = "0000-0002-1825-0097"


@pytest.fixture
def no_backoff(monkeypatch):
    """with_retries sleeps through http_pacing._sleep; skip it."""
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(http_pacing, "_sleep", _instant)


def _summary(title, *, idx="0", funder="NIH", start=("2019", "07"), end=("2024", None), ids=None):
    s = {"title": {"title": {"value": title}}, "organization": {"name": funder}, "type": "GRANT",
         "display-index": idx,
         "start-date": {"year": {"value": start[0]}, "month": {"value": start[1]}}}
    s["end-date"] = None if end is None else {
        "year": {"value": end[0]}, "month": end[1] and {"value": end[1]}}
    if ids is not None:
        s["external-ids"] = {"external-id": ids}
    return s


def test_the_lowest_display_index_supplies_the_fields():
    data = {"group": [{"external-ids": {"external-id": [
        {"external-id-type": "grant_number", "external-id-value": "R01 GM-123456",
         "external-id-relationship": "self"}]},
        "funding-summary": [_summary("Second", idx="1"), _summary("First", idx="0")]}]}
    [f] = of.parse_orcid_fundings(data)
    got = (f.title, f.funder_name, f.funding_type, f.start_year, f.start_month, f.end_year,
           f.end_month)
    assert got == ("First", "NIH", "grant", 2019, 7, 2024, None)
    assert f.group_key == "ext:grant_number:R01GM123456"


def test_a_group_full_of_nulls_does_not_raise():
    data = {"group": [None, {"funding-summary": None}, {"external-ids": None, "funding-summary": [
        {"title": None, "organization": None, "start-date": None, "end-date": None}]},
        {"funding-summary": [{"title": {"title": {"value": "Only title"}}, "organization": None,
                              "start-date": {"year": None}, "end-date": {"year": {"value": "x"}},
                              "external-ids": {"external-id": None}, "display-index": None}]}]}
    [f] = of.parse_orcid_fundings(data)
    assert f.title == "Only title" and f.funder_name is None
    assert f.start_year is None and f.end_year is None
    assert f.group_key.startswith("hash:")
    assert of.parse_orcid_fundings(None) == [] and of.parse_orcid_fundings({"group": None}) == []


def test_a_long_identifier_is_hashed_into_the_column():
    f, = of.parse_orcid_fundings({"group": [{"funding-summary": [_summary("T", ids=[
        {"external-id-type": "grant_number", "external-id-value": "X" * 300}])]}]})
    assert len(f.group_key) <= of.GROUP_KEY_MAX and f.group_key.startswith("ext:sha1:")


def test_out_of_range_years_read_as_unknown():
    f, = of.parse_orcid_fundings({"group": [{"funding-summary": [
        _summary("T", start=("0019", "13"), end=("20190", None))]}]})
    assert (f.start_year, f.start_month, f.end_year) == (None, None, None)


@respx.mock
async def test_strict_fetch_raises_on_5xx_and_reads_record_state_as_none(no_backoff):
    respx.get(f"{BASE}/{OID}/fundings").mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await of.fetch_orcid_fundings(OID, strict=True)
    for status in (301, 404, 409, 410):
        respx.get(f"{BASE}/{OID}/fundings").mock(return_value=httpx.Response(status))
        assert await of.fetch_orcid_fundings(OID, strict=True) == []


@respx.mock
async def test_soft_fetch_returns_none_on_failure(no_backoff):
    respx.get(f"{BASE}/{OID}/fundings").mock(side_effect=httpx.ConnectTimeout("t"))
    assert await of.fetch_orcid_fundings(OID, strict=False) is None
    respx.get(f"{BASE}/{OID}/fundings").mock(return_value=httpx.Response(200, content=b"not json"))
    assert await of.fetch_orcid_fundings(OID, strict=False) is None


def _f(key, title, end_year):
    return of.OrcidFunding(key, title, "F", "grant", 2010, None, end_year, None, ())


async def _rows(db_session, user_id):
    return (await db_session.execute(select(PiOrcidFunding).where(
        PiOrcidFunding.user_id == user_id).execution_options(populate_existing=True))).scalars().all()


@pytest.mark.integration
async def test_duplicate_group_keys_collapse_before_upsert(db_session):
    pi = await factories.make_user(db_session)
    await of.store_orcid_fundings(db_session, pi.id, [_f("k", "Older", 2015), _f("k", "Newer", 2020)])
    assert [(r.group_key, r.title) for r in await _rows(db_session, pi.id)] == [("k", "Newer")]


@pytest.mark.integration
async def test_refresh_keeps_vetoes_and_deletes_unreturned_rows_and_stamps(db_session):
    pi = await factories.make_user(db_session)
    staff = await factories.make_user(db_session, user_role="manager")
    await of.store_orcid_fundings(
        db_session, pi.id, [_f("a", "A", 2020), _f("b", "B", 2020), _f("c", "C", 2020)])
    vetoed = (await db_session.execute(select(PiOrcidFunding).where(
        PiOrcidFunding.user_id == pi.id, PiOrcidFunding.group_key == "a"))).scalar_one()
    vetoed.vetoed_at, vetoed.vetoed_by_user_id = datetime.now(UTC), staff.id
    await db_session.flush()
    await of.store_orcid_fundings(db_session, pi.id, [_f("a", "A renamed", 2021), _f("b", "B", 2020)])
    rows = {r.group_key: r for r in await _rows(db_session, pi.id)}
    assert set(rows) == {"a", "b"}
    assert rows["a"].vetoed_by_user_id == staff.id and rows["a"].title == "A renamed"
    await of.store_orcid_fundings(db_session, pi.id, [])
    left = [r.group_key for r in await _rows(db_session, pi.id)]
    assert left == ["a"], "a refresh never deletes a vetoed row"
    identity = await db_session.get(PiGrantIdentity, pi.id)
    assert identity.orcid_fetched_at is not None and identity.status is None
