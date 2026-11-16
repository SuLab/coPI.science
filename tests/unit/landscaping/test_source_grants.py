"""The RePORTER and NSF grant sources (plan Task 3.3, spec 2026-10-30 §6.1). No network:
RePORTER's `search_projects` is replaced, NSF runs over an httpx.MockTransport."""
from datetime import date

import httpx

from src.landscaping.institutions import NSF_AWARDEE, REPORTER_ORGS
from src.landscaping.sources import nsf, reporter
from src.services import nih_reporter
from src.services.http_pacing import Pacer


def _row(appl_id, core, *, award, subproject=None, pis=None, fy=2025):
    return {
        "appl_id": appl_id, "project_num": f"5{core}-0{appl_id}", "core_project_num": core,
        "fiscal_year": fy, "project_title": f"Project {core}", "abstract_text": "An abstract.",
        "phr_text": "Public health relevance.", "organization": {"org_name": REPORTER_ORGS[0]},
        "principal_investigators": pis or [], "project_start_date": "2023-07-01T00:00:00",
        "project_end_date": "2028-06-30T00:00:00", "award_amount": award,
        "subproject_id": subproject, "activity_code": "R01",
    }


def _total(n):
    async def reported(criteria):
        return n
    return reported


async def test_reporter_expected_is_reporters_own_total(monkeypatch):
    async def fake_search(criteria, include_fields, *, max_total=500):
        return [_row(11, "R01CA000001", award=100)]

    monkeypatch.setattr(nih_reporter, "search_projects", fake_search)
    monkeypatch.setattr(reporter, "_reported_total", _total(4))
    result = await reporter.fetch_fiscal_year(2025)

    assert (result.expected, result.received) == (4, 1)


async def test_reporter_criteria_and_fields(monkeypatch):
    calls = []

    async def fake_search(criteria, include_fields, *, max_total=500):
        calls.append((criteria, include_fields, max_total))
        return []

    monkeypatch.setattr(nih_reporter, "search_projects", fake_search)
    monkeypatch.setattr(reporter, "_reported_total", _total(0))
    result = await reporter.fetch_fiscal_year(2025)

    assert calls == [(
        {"org_names_exact_match": list(REPORTER_ORGS), "fiscal_years": [2025]},
        nih_reporter.PROJECT_FIELDS + ["AbstractText", "PrefTerms", "ApplId"],
        5000,
    )]
    assert (result.rows, result.expected, result.received) == ([], 0, 0)


async def test_reporter_groups_by_core_project(monkeypatch):
    contact = {"profile_id": 7001, "full_name": "Ada Q. Fictitious", "is_contact_pi": True}
    other = {"profile_id": 7002, "full_name": "Bo Imaginary", "is_contact_pi": False}
    rows = [
        _row(11, "R01CA000001", award=100, pis=[other, contact]),
        _row(12, "R01CA000001", award=50, subproject="0001", pis=[other]),
        _row(21, "R21NS000002", award=80, pis=[other]),
    ]

    async def fake_search(criteria, include_fields, *, max_total=500):
        return rows

    monkeypatch.setattr(nih_reporter, "search_projects", fake_search)
    monkeypatch.setattr(reporter, "_reported_total", _total(3))
    result = await reporter.fetch_fiscal_year(2025)

    assert result.expected == 3 and result.received == 3
    assert len(result.rows) == 2
    grouped = next(r for r in result.rows if r.ids["core_project"] == "R01CA000001")
    assert grouped.kind == "grant" and grouped.role_basis == "grant_pi"
    assert grouped.ids["appl_id"] == "11"
    assert grouped.extra["appl_ids"] == ["11", "12"]
    assert grouped.extra["award_amount"] == 150
    assert grouped.summary == "An abstract." and grouped.summary_source == "reporter"
    assert grouped.published == date(2023, 7, 1)
    assert grouped.senior_names == [{"name": "Ada Q. Fictitious", "orcid": None,
                                     "openalex_author_id": None, "nih_pi_profile_id": "7001"}]
    single = next(r for r in result.rows if r.ids["core_project"] == "R21NS000002")
    assert single.senior_names[0]["nih_pi_profile_id"] == "7002"


def _award(i):
    return {"id": str(2400000 + i), "title": f"Award {i}", "abstractText": "Abstract.",
            "awardeeName": NSF_AWARDEE, "piFirstName": "Cy", "piLastName": "Madeup",
            "date": "03/15/2025", "startDate": "04/01/2025", "expDate": "03/31/2028"}


async def test_nsf_paging_rpp_25(monkeypatch):
    seen = []

    def handler(request):
        params = dict(request.url.params)
        seen.append(params)
        offset = int(params["offset"])
        count = 25 if offset == 1 else 5
        awards = [_award(offset + k) for k in range(count)]
        return httpx.Response(200, json={"response": {
            "metadata": {"totalCount": 30}, "award": awards}})

    monkeypatch.setattr(nsf, "_make_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(nsf, "_PACER", Pacer(0))
    result = await nsf.fetch_nsf(date(2025, 1, 1), date(2025, 6, 30))

    assert [p["offset"] for p in seen] == ["1", "26"]
    assert all(p["rpp"] == "25" for p in seen)
    assert seen[0]["awardeeName"] == f'"{NSF_AWARDEE}"'
    assert (seen[0]["dateStart"], seen[0]["dateEnd"]) == ("01/01/2025", "06/30/2025")
    assert (result.expected, result.received, len(result.rows)) == (30, 30, 30)
    first = result.rows[0]
    assert first.kind == "grant" and first.role_basis == "grant_pi"
    assert first.ids == {"nsf_award": "2400001"}
    assert first.published == date(2025, 3, 15)
    assert first.senior_names[0]["name"] == "Cy Madeup"
