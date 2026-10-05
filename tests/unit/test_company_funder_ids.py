import httpx
import pytest
import respx

from src.services.industry_sources import openalex_industry as oa

pytestmark = pytest.mark.asyncio


@respx.mock
async def test_one_institutions_request_per_chunk_and_same_answer(monkeypatch):
    monkeypatch.setattr(oa, "get_settings", lambda: type("S", (), {"ncbi_contact_email": None})())
    funders = respx.get(f"{oa.OA}/funders").mock(return_value=httpx.Response(200, json={"results": [
        {"id": "https://openalex.org/F1", "roles": [{"role": "institution", "id": "https://openalex.org/I1"}]},
        {"id": "https://openalex.org/F2", "roles": [{"role": "institution", "id": "https://openalex.org/I2"}]},
        {"id": "https://openalex.org/F3", "roles": [{"role": "funder", "id": "https://openalex.org/F3"}]},
    ]}))
    insts = respx.get(f"{oa.OA}/institutions").mock(return_value=httpx.Response(200, json={"results": [
        {"id": "https://openalex.org/I1", "type": "company"},
        {"id": "https://openalex.org/I2", "type": "education"},
    ]}))
    assert await oa.company_funder_ids({"F1", "F2", "F3"}) == {"F1"}
    assert funders.call_count == 1 and insts.call_count == 1


async def test_empty_input_makes_no_request():
    assert await oa.company_funder_ids(set()) == set()


@respx.mock
async def test_a_429_is_retried_with_backoff_instead_of_failing_the_job(monkeypatch):
    """52 industry jobs died on 2026-09-22/25 of one unretried OpenAlex 429 each."""
    from src.services import http_pacing

    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(http_pacing, "_sleep", fake_sleep)
    monkeypatch.setattr(oa, "get_settings", lambda: type("S", (), {"ncbi_contact_email": None})())
    oa._PACER.reset()
    works = respx.get(f"{oa.OA}/works").mock(side_effect=[
        httpx.Response(429), httpx.Response(200, json={"results": [{"id": "https://openalex.org/W1"}]}),
    ])
    assert await oa.fetch_works_for_pmids(["31980915"]) == [{"id": "https://openalex.org/W1"}]
    assert works.call_count == 2
    assert slept == [2.0]
