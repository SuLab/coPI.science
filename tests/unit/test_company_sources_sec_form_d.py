"""SEC Form D funding (spec §7.5 Step 2, O12, O13, §8, Review Focus #1 and #2).

Fixtures, recorded raw from SEC EDGAR with curl on 2026-10-02: DELFI Diagnostics' 2022
Form D 0001783735-22-000002, and C2N Diagnostics' Form D 0002021597-24-000001, its
amendment D/A 0002021597-24-000002 and a later, separate offering 0002021597-25-000002,
with the EDGAR full-text search answers that list them. Variants (another issuer, a
blank amount, an oversize body) are derived from them in-test.
"""
import json
import re
from datetime import date
from pathlib import Path

import httpx
import pytest

from src.services.company_sources import pi_name, sec_form_d
from src.services.pi_companies import normalize_company_name

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "company_discovery"
UA = "Blackbird Labs ops@example.org"
DELFI_2022 = "https://www.sec.gov/Archives/edgar/data/1783735/000178373522000002/primary_doc.xml"
C2N_DOCS = {
    "0002021597-24-000001": "https://www.sec.gov/Archives/edgar/data/2021597/000202159724000001/primary_doc.xml",
    "0002021597-24-000002": "https://www.sec.gov/Archives/edgar/data/2021597/000202159724000002/primary_doc.xml",
    "0002021597-25-000002": "https://www.sec.gov/Archives/edgar/data/2021597/000202159725000002/primary_doc.xml",
}


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(sec_form_d, "_interval", lambda: 0.0)
    sec_form_d._PACER.reset()


def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _search(name: str, *, only: str | None = None) -> dict:
    data = json.loads(_text(name))
    if only is not None:
        data["hits"]["hits"] = [h for h in data["hits"]["hits"] if h["_source"]["adsh"] == only]
    return data


def _delfi_routes(respx_mock, doc_text: str | None = None):
    search = respx_mock.get(sec_form_d.EFTS_URL).mock(return_value=httpx.Response(
        200, json=_search("efts_delfi_diagnostics.json", only="0001783735-22-000002")))
    doc = respx_mock.get(DELFI_2022).mock(return_value=httpx.Response(
        200, text=doc_text if doc_text is not None else _text("form_d_delfi_000178373522000002.xml")))
    return search, doc


def _c2n_routes(respx_mock, edits: dict[str, tuple[str, str]] | None = None):
    """C2N's three filings; `edits` maps an accession to one (old, new) text replacement."""
    respx_mock.get(sec_form_d.EFTS_URL).mock(
        return_value=httpx.Response(200, json=_search("efts_c2n_diagnostics.json")))
    for accession, url in C2N_DOCS.items():
        text = _text(f"form_d_c2n_{accession.replace('-', '')}.xml")
        if edits and accession in edits:
            old, new = edits[accession]
            assert old in text
            text = text.replace(old, new)
        respx_mock.get(url).mock(return_value=httpx.Response(200, text=text))


async def _delfi(pi: str = "Victor Velculescu") -> sec_form_d.FundingResult:
    return await sec_form_d.lookup_funding(
        "DELFI Diagnostics", normalize_company_name("DELFI Diagnostics"), pi_name(pi), user_agent=UA)


async def test_delfi_2022_is_read_and_the_pi_is_listed(respx_mock):
    search, _doc = _delfi_routes(respx_mock)
    result = await _delfi()
    assert result.status == "ok"
    assert result.funding_usd == 224_999_876 and result.funding_as_of == date(2022, 7, 25)
    assert result.funding_source_url == (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001783735"
        "&type=D&dateb=&owner=include&count=40"
    )
    (filing,) = result.filings
    assert filing.accession == "0001783735-22-000002" and filing.form == "D"
    assert filing.issuer_name == "Delfi Diagnostics, Inc." and filing.is_amendment is False
    assert filing.total_offering_amount == 224_999_876 and filing.total_amount_sold == 224_999_876
    assert filing.pi_listed is True
    assert filing.pi_relationships == ["Executive Officer", "Director", "Promoter"]
    assert filing.counted is True
    request = search.calls.last.request
    assert request.headers["User-Agent"] == UA
    assert dict(request.url.params) == {"q": '"DELFI Diagnostics"', "forms": "D"}
    assert search.call_count == 1


async def test_an_amended_offering_is_counted_once(respx_mock):
    _c2n_routes(respx_mock)
    result = await sec_form_d.lookup_funding(
        "C2N Diagnostics", normalize_company_name("C2N Diagnostics"), pi_name("Randall Bateman"),
        user_agent=UA)
    by_acc = {f.accession: f for f in result.filings}
    amendment = by_acc["0002021597-24-000002"]
    assert amendment.form == "D/A" and amendment.is_amendment is True
    assert amendment.previous_accession == "0002021597-24-000001"
    assert [a for a, f in sorted(by_acc.items()) if f.counted] == [
        "0002021597-24-000002", "0002021597-25-000002"]
    assert result.funding_usd == 24_999_618 + 9_999_891  # not + 7,500,000 from the original D
    assert result.funding_as_of == date(2025, 5, 30)
    assert all(f.pi_listed and f.pi_relationships == ["Director"] for f in result.filings)


def _filing(acc, filed, sold, previous=None):
    return sec_form_d.FormDFiling(
        accession=acc, filing_date=filed, form="D/A" if previous else "D", cik="1",
        file_num=None, issuer_name="X", is_amendment=previous is not None,
        previous_accession=previous, total_offering_amount=None, total_offering_amount_raw=None,
        total_amount_sold=sold, total_amount_sold_raw=None if sold is None else str(sold),
        amount_note=None if sold is not None else "totalAmountSold blank; not counted",
        pi_listed=False, pi_relationships=[], url="u")


def test_amendment_chain_without_a_file_number_still_groups():
    filings = [_filing("0000000001-20-000001", "2020-01-01", 100),
               _filing("0000000001-20-000002", "2020-06-01", 300, previous="0000000001-20-000001"),
               _filing("0000000001-21-000001", "2021-01-01", 50)]
    assert sec_form_d.floor_total(filings) == (350, date(2021, 1, 1))


def test_a_latest_amendment_without_an_amount_never_falls_back_to_the_original():
    original = _filing("0000000001-20-000001", "2020-01-01", 100)
    amendment = _filing("0000000001-20-000002", "2020-06-01", None, previous="0000000001-20-000001")
    assert sec_form_d.floor_total([original, amendment]) == (None, None)
    assert original.counted is False and amendment.counted is False
    assert amendment.amount_note == (
        "totalAmountSold blank; not counted; latest filing of its offering, so the offering is not counted")


async def test_an_offering_whose_latest_amendment_is_blank_contributes_nothing(respx_mock):
    _c2n_routes(respx_mock, {"0002021597-24-000002": (
        "<totalAmountSold>24999618</totalAmountSold>", "<totalAmountSold></totalAmountSold>")})
    result = await sec_form_d.lookup_funding(
        "C2N Diagnostics", normalize_company_name("C2N Diagnostics"), pi_name("Randall Bateman"),
        user_agent=UA)
    by_acc = {f.accession: f for f in result.filings}
    assert [a for a, f in sorted(by_acc.items()) if f.counted] == ["0002021597-25-000002"]
    assert result.status == "ok" and result.funding_usd == 9_999_891  # not + 7,500,000 from the D
    assert by_acc["0002021597-24-000002"].amount_note == (
        "totalAmountSold blank; not counted; latest filing of its offering, so the offering is not counted")


@pytest.mark.parametrize(("old", "new", "note"), [
    ("<totalAmountSold>224999876</totalAmountSold>", "<totalAmountSold>Indefinite</totalAmountSold>",
     "totalAmountSold 'Indefinite'; not counted"),
    ("<totalAmountSold>224999876</totalAmountSold>", "<totalAmountSold></totalAmountSold>",
     "totalAmountSold blank; not counted"),
    ("<totalAmountSold>224999876</totalAmountSold>", "", "totalAmountSold missing; not counted"),
])
async def test_an_unusable_amount_sold_is_never_counted_as_zero(respx_mock, old, new, note):
    _delfi_routes(respx_mock, _text("form_d_delfi_000178373522000002.xml").replace(old, new))
    result = await _delfi()
    assert result.status == "no_amount" and result.funding_usd is None and result.funding_as_of is None
    (filing,) = result.filings
    assert filing.total_amount_sold is None and filing.counted is False
    assert filing.amount_note.startswith(note)


async def test_an_indefinite_offering_amount_still_counts_what_was_sold(respx_mock):
    doc = _text("form_d_delfi_000178373522000002.xml").replace(
        "<totalOfferingAmount>224999876</totalOfferingAmount>",
        "<totalOfferingAmount>Indefinite</totalOfferingAmount>")
    _delfi_routes(respx_mock, doc)
    result = await _delfi()
    (filing,) = result.filings
    assert filing.total_offering_amount is None and filing.total_offering_amount_raw == "Indefinite"
    assert filing.amount_note == "totalOfferingAmount 'Indefinite'"
    assert result.funding_usd == 224_999_876 and filing.counted is True


async def test_the_issuer_name_gate_drops_another_issuers_filing(respx_mock):
    _delfi_routes(respx_mock, _text("form_d_c2n_000202159724000001.xml"))
    result = await _delfi()
    assert result.status == "no_filings" and result.filings == [] and result.funding_usd is None


@pytest.mark.parametrize(("pi", "first", "last", "listed"), [
    ("Victor Velculescu", "Victor", "Velculescu", True),
    ("Victor Velculescu", "VICTOR", "VELCULESCU", True),
    ("Victor Velculescu", "Víctor", "Velculescú", True),
    ("Victor Velculescu", "Dr. Victor", "Velculescu", True),
    ("Vlad Velculescu", "Victor", "Velculescu", False),
    ("Vic Velculescu", "Victor", "Velculescu", False),
    ("Anne Hamacher Brady", "Anne", "Hamacher-Brady", True),
    ("Annette Hamacher-Brady", "Anne", "Hamacher-Brady", False),
    ("Hans Mueller", "Hans", "Müller", True),
])
async def test_related_person_match_is_exact_after_folding(respx_mock, pi, first, last, listed):
    doc, n = re.subn(
        r"<firstName>Victor</firstName>(\s*)<lastName>Velculescu</lastName>",
        lambda m: f"<firstName>{first}</firstName>{m.group(1)}<lastName>{last}</lastName>",
        _text("form_d_delfi_000178373522000002.xml"))
    assert n == 1
    _delfi_routes(respx_mock, doc)
    (filing,) = (await _delfi(pi)).filings
    assert filing.pi_listed is listed


async def test_a_particle_surname_in_the_real_filing_matches(respx_mock):
    _delfi_routes(respx_mock)
    (filing,) = (await _delfi("Jacob Van Naarden")).filings
    assert filing.pi_listed is True and filing.pi_relationships == ["Director"]


_ACME = {"0001783735-22-000002": ("0001783735", "Acme Bio, Inc.", "<totalAmountSold>224999876</totalAmountSold>"),
         "0001999999-23-000001": ("0001999999", "Acme Bio LLC", "<totalAmountSold>5000000</totalAmountSold>")}


def _acme_routes(respx_mock, pi_listed_at: tuple[str, ...]):
    """Two issuers named "Acme Bio" under different CIKs, built from the DELFI filing;
    Victor Velculescu stays a related person only in the filings of `pi_listed_at`."""
    template = _search("efts_delfi_diagnostics.json", only="0001783735-22-000002")["hits"]["hits"][0]
    hits = []
    for n, (accession, (cik, issuer, sold)) in enumerate(_ACME.items()):
        hits.append({**template, "_id": f"{accession}:primary_doc.xml",
                     "_source": {**template["_source"], "adsh": accession, "ciks": [cik],
                                 "file_date": f"202{2 + n}-07-25", "file_num": [f"021-90{n}"]}})
        doc = _text("form_d_delfi_000178373522000002.xml").replace(
            "<entityName>Delfi Diagnostics, Inc.</entityName>", f"<entityName>{issuer}</entityName>").replace(
            "<totalAmountSold>224999876</totalAmountSold>", sold)
        if cik not in pi_listed_at:
            doc = doc.replace("<lastName>Velculescu</lastName>", "<lastName>Someone</lastName>")
        url = sec_form_d.ARCHIVE_DOC_URL.format(
            cik=int(cik), accession=accession.replace("-", ""), filename="primary_doc.xml")
        respx_mock.get(url).mock(return_value=httpx.Response(200, text=doc))
    respx_mock.get(sec_form_d.EFTS_URL).mock(return_value=httpx.Response(200, json={"hits": {"hits": hits}}))


async def _acme() -> sec_form_d.FundingResult:
    return await sec_form_d.lookup_funding(
        "Acme Bio", normalize_company_name("Acme Bio"), pi_name("Victor Velculescu"), user_agent=UA)


@pytest.mark.parametrize("pi_listed_at", [(), ("0001783735", "0001999999")])
async def test_several_issuers_under_one_name_are_not_summed(respx_mock, pi_listed_at):
    _acme_routes(respx_mock, pi_listed_at)
    result = await _acme()
    assert result.status == "ambiguous" and result.funding_usd is None and result.funding_as_of is None
    assert result.funding_source_url is None and not any(f.counted for f in result.filings)
    evidence = result.evidence()
    assert evidence["note"] == sec_form_d.AMBIGUOUS_ISSUER
    assert evidence["ciks"] == ["0001783735", "0001999999"] and evidence["issuer_cik"] is None
    assert len(evidence["filings"]) == 2


async def test_the_issuer_listing_the_pi_is_the_one_counted(respx_mock):
    _acme_routes(respx_mock, ("0001999999",))
    result = await _acme()
    assert result.status == "ok" and result.funding_usd == 5_000_000
    assert result.funding_as_of == date(2023, 7, 25)
    assert result.funding_source_url == sec_form_d.filings_page_url("0001999999")
    assert [f.cik for f in result.filings if f.counted] == ["0001999999"]
    evidence = result.evidence()
    assert evidence["issuer_cik"] == "0001999999" and evidence["ciks"] == ["0001783735", "0001999999"]
    assert "note" not in evidence


@pytest.mark.parametrize("which", ["search", "document"])
async def test_a_redirect_is_unavailable_and_not_followed(respx_mock, which):
    search_route, doc_route = _delfi_routes(respx_mock)
    route = search_route if which == "search" else doc_route
    route.mock(return_value=httpx.Response(302, headers={"Location": "https://example.org/elsewhere"}))
    result = await _delfi()
    assert result.status == "unavailable" and result.reason == "HTTP 302"
    assert len(respx_mock.calls) == (1 if which == "search" else 2)
    assert all(call.request.url.host in ("efts.sec.gov", "www.sec.gov") for call in respx_mock.calls)


async def test_an_oversize_search_answer_is_unavailable(respx_mock):
    search_route, doc_route = _delfi_routes(respx_mock)
    search_route.mock(return_value=httpx.Response(200, content=b" " * (sec_form_d.MAX_RESPONSE_BYTES + 1)))
    result = await _delfi()
    assert result.status == "unavailable" and result.reason == "response too large"
    assert doc_route.call_count == 0


async def test_an_oversize_document_without_a_declared_length_is_unavailable(respx_mock, monkeypatch):
    monkeypatch.setattr(sec_form_d, "MAX_RESPONSE_BYTES", 4096)
    _search_route, doc_route = _delfi_routes(respx_mock)
    oversize = httpx.Response(200, text=_text("form_d_delfi_000178373522000002.xml"))
    del oversize.headers["Content-Length"]  # only the bytes received can trip the cap
    doc_route.mock(return_value=oversize)
    result = await _delfi()
    assert result.status == "unavailable" and result.reason == "response too large"
    assert result.evidence()["note"] == "funding lookup unavailable"


async def test_the_client_does_not_follow_redirects():
    async with sec_form_d._make_client() as client:
        assert client.follow_redirects is False


async def test_unset_user_agent_sends_nothing(respx_mock):
    route = respx_mock.get(sec_form_d.EFTS_URL)
    result = await sec_form_d.lookup_funding("DELFI Diagnostics", "delfi diagnostics", None, user_agent="  ")
    assert result.status == "unavailable" and route.call_count == 0
    assert result.evidence()["note"] == "funding lookup unavailable"
    assert result.evidence()["reason"] == "SEC_USER_AGENT unset"


@pytest.mark.parametrize(("search", "doc", "reason"), [
    (httpx.Response(429), None, "HTTP 429"),
    (httpx.ReadTimeout("slow"), None, "ReadTimeout"),
    (httpx.Response(200, text="<html>rate limited</html>"), None, "unreadable search response"),
    (None, httpx.Response(404), "HTTP 404"),
    (None, httpx.Response(200, text="<html>not xml"), "unreadable document"),
    (None, httpx.Response(200, text='<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "a">]><x/>'),
     "unreadable document (DTD present)"),
    (None, httpx.Response(200, text="<other/>"), "unreadable document (not a Form D)"),
])
async def test_any_failure_leaves_the_candidate_unavailable(respx_mock, search, doc, reason):
    search_route, doc_route = _delfi_routes(respx_mock)
    if search is not None:
        if isinstance(search, Exception):
            search_route.mock(side_effect=search)
        else:
            search_route.mock(return_value=search)
    if doc is not None:
        doc_route.mock(return_value=doc)
    result = await _delfi()
    assert result.status == "unavailable" and result.reason == reason
    assert result.funding_usd is None and result.filings == []
    assert result.evidence()["note"] == "funding lookup unavailable"


async def test_documents_are_capped_newest_first(respx_mock):
    template = _search("efts_delfi_diagnostics.json", only="0001783735-22-000002")["hits"]["hits"][0]
    hits = []
    for n in range(sec_form_d.MAX_DOCUMENTS + 3):
        accession = f"0001783735-22-{n:06d}"
        hits.append({**template, "_id": f"{accession}:primary_doc.xml",
                     "_source": {**template["_source"], "adsh": accession,
                                 "file_date": f"2022-01-{n + 1:02d}", "file_num": [f"021-{n}"]}})
    respx_mock.get(sec_form_d.EFTS_URL).mock(return_value=httpx.Response(200, json={"hits": {"hits": hits}}))
    docs = respx_mock.get(url__regex=r"https://www\.sec\.gov/Archives/edgar/data/1783735/\d+/primary_doc\.xml").mock(
        return_value=httpx.Response(200, text=_text("form_d_delfi_000178373522000002.xml")))
    result = await _delfi()
    assert docs.call_count == sec_form_d.MAX_DOCUMENTS and result.not_fetched == 3
    assert min(f.filing_date for f in result.filings) == "2022-01-04"


@pytest.mark.parametrize(("raw", "value", "note"), [
    ("224999876", 224_999_876, None), (" 1,500,000 ", 1_500_000, None), ("$2500.00", 2500, None),
    ("Indefinite", None, "x 'Indefinite'"), ("indefinite", None, "x 'Indefinite'"),
    ("", None, "x blank"), (None, None, "x missing"), ("-1", None, "x unreadable ('-1')"),
])
def test_parse_amount(raw, value, note):
    assert sec_form_d.parse_amount(raw, "x") == (value, note)
