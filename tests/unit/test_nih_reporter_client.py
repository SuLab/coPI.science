import httpx
import pytest
import respx

from src.services import nih_reporter as nr

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
