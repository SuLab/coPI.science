import json

import httpx
import pytest
import respx

from src.services import nih_reporter as nr
from src.services.http_pacing import Pacer

pytestmark = pytest.mark.contract
URL = "https://api.reporter.nih.gov/v2/projects/search"
PUBS = "https://api.reporter.nih.gov/v2/publications/search"


def _page(total, rows, offset=0):
    return {"meta": {"total": total, "offset": offset, "limit": 500}, "results": rows}


async def test_unknown_criteria_key_is_refused_before_any_request():
    with respx.mock(assert_all_called=False) as router:
        route = router.post(URL)
        with pytest.raises(nr.ReporterCriteriaError):
            await nr.search_projects({"orcid": ["0000-0002-2214-0114"]}, nr.PROJECT_FIELDS)
        assert not route.called


async def test_firehose_total_aborts():
    with respx.mock() as router:
        router.post(URL).mock(return_value=httpx.Response(200, json=_page(2975461, [{"project_num": "x"}])))
        with pytest.raises(nr.ReporterFirehoseError):
            await nr.search_projects({"pi_names": [{"last_name": "Wu"}]}, nr.PROJECT_FIELDS)


async def test_pages_until_total_reached():
    rows1 = [{"project_num": f"a{i}"} for i in range(500)]
    rows2 = [{"project_num": "b0"}]
    with respx.mock() as router:
        router.post(URL).mock(side_effect=[
            httpx.Response(200, json=_page(501, rows1, 0)),
            httpx.Response(200, json=_page(501, rows2, 500)),
        ])
        out = await nr.search_projects({"pi_profile_ids": [9751245]}, nr.PROJECT_FIELDS, max_total=1000)
    assert len(out) == 501


async def test_publications_for_cores_groups_pmids_as_strings():
    with respx.mock() as router:
        router.post(PUBS).mock(return_value=httpx.Response(200, json={
            "meta": {"total": 2, "offset": 0, "limit": 500},
            "results": [{"coreproject": "R01AI137329", "pmid": 34187885, "applid": 1},
                        {"coreproject": "R01AI137329", "pmid": 30000000, "applid": 2}]}))
        out = await nr.publications_for_cores(["R01AI137329"])
    assert out == {"R01AI137329": {"34187885", "30000000"}}


def _offset_capped(rows_for):
    """A publications/search side effect that refuses an offset past ``MAX_OFFSET`` with
    RePORTER's own 400, and otherwise answers ``rows_for(cores, offset)`` -> (total, rows)."""
    def answer(request):
        body = json.loads(request.content)
        if body["offset"] > nr.MAX_OFFSET:
            return httpx.Response(400, json={"message": "System doesn't support offset value "
                                             "greater than 9,999. Please narrow down your "
                                             "search criteria."})
        total, rows = rows_for(tuple(body["criteria"]["core_project_nums"]), body["offset"])
        return httpx.Response(200, json=_page(total, rows, body["offset"]))
    return answer


async def test_publications_search_past_the_offset_cap_is_split(monkeypatch):
    """A 50-project chunk listed 12,523 publications on 2026-11-16 and paging hit RePORTER's
    offset cap; such a search is split in half and each half searched again."""
    monkeypatch.setattr(nr, "_PACER", Pacer(0))
    asked = []

    def rows_for(cores, offset):
        asked.append((cores, offset))
        if cores == ("P30AA000001", "R01AA000002"):
            return 12_000, []
        return 1, [{"coreproject": cores[0], "pmid": 7 if cores[0].startswith("P30") else 8}]

    with respx.mock() as router:
        router.post(PUBS).mock(side_effect=_offset_capped(rows_for))
        out = await nr.publications_for_cores(["P30AA000001", "R01AA000002"])

    assert out == {"P30AA000001": {"7"}, "R01AA000002": {"8"}}
    assert asked == [(("P30AA000001", "R01AA000002"), 0), (("P30AA000001",), 0),
                     (("R01AA000002",), 0)]


async def test_one_project_past_the_offset_cap_keeps_what_paging_reaches(monkeypatch, caplog):
    monkeypatch.setattr(nr, "_PACER", Pacer(0))

    def rows_for(cores, offset):
        return 12_523, [{"coreproject": cores[0], "pmid": offset + 1}]

    with respx.mock() as router:
        route = router.post(PUBS).mock(side_effect=_offset_capped(rows_for))
        out = await nr.publications_for_cores(["P30AA000001"])

    assert route.call_count == 20  # offsets 0 .. 9,500; never 10,000
    assert len(out["P30AA000001"]) == 20
    assert "12523 publications for P30AA000001" in caplog.text


async def test_core_project_nums_is_refused_on_projects_search():
    """projects/search silently ignores core_project_nums (NEW-2); before the per-endpoint
    split it was allowed there and returned the whole database."""
    with respx.mock(assert_all_called=False) as router:
        route = router.post(URL)
        with pytest.raises(nr.ReporterCriteriaError):
            await nr.search_projects({"core_project_nums": ["R01AI137329"]}, nr.PROJECT_FIELDS)
        assert not route.called


def test_publications_allowlist_is_its_own():
    with pytest.raises(nr.ReporterCriteriaError):
        nr._check_criteria({"pi_names": []}, nr.PUBLICATIONS_CRITERIA, "publications/search")
    nr._check_criteria({"core_project_nums": ["X"]}, nr.PUBLICATIONS_CRITERIA, "publications/search")


async def test_profile_id_exists_reads_one_page():
    with respx.mock() as router:
        route = router.post(URL).mock(return_value=httpx.Response(200, json=_page(3, [
            {"principal_investigators": [{"profile_id": 9751245}]}])))
        assert await nr.profile_id_exists(9751245) is True
        body = route.calls[0].request.content
        assert body and b'"limit":1' in body.replace(b" ", b"")
    with respx.mock() as router:
        router.post(URL).mock(return_value=httpx.Response(200, json=_page(2983191, [
            {"principal_investigators": [{"profile_id": 1}]}])))
        assert await nr.profile_id_exists(9751245) is False
