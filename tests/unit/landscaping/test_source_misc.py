"""The patent, JHTV and dataset sources (plan Task 3.4, spec 2026-10-30 §6.1). No network:
HTTP runs over an httpx.MockTransport and NCBI's `_ncbi_get` is replaced."""
import json
from datetime import date

import httpx

from src.landscaping.sources import datacite, geo, jhtv, odp_patents
from src.services import pubmed
from src.services.http_pacing import Pacer

_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:tp="https://example.org/technologypublisher">
  <channel>
    <title>Technologies</title>
    <item>
      <title>A fictitious kinase inhibitor</title>
      <link>https://jhu.technologypublisher.com/tech?title=A_fictitious&amp;caseId=C10001</link>
      <guid isPermaLink="false">guid-0001</guid>
      <pubDate>Tue, 14 Oct 2025 09:30:00 GMT</pubDate>
      <author>inventor@example.org (Ada Fictitious)</author>
      <description>&lt;p&gt;A &lt;b&gt;small molecule&lt;/b&gt; that does things.&lt;/p&gt;</description>
      <category>Therapeutics</category>
    </item>
    <item>
      <title>An imaginary antibody</title>
      <link>https://jhu.technologypublisher.com/tech/An_imaginary</link>
      <guid>guid-0002</guid>
      <tp:caseId>C10002</tp:caseId>
      <pubDate>Mon, 03 Mar 2025 12:00:00 -0500</pubDate>
      <author>Bo Imaginary</author>
      <description>Binds a target.</description>
    </item>
  </channel>
</rss>
"""


async def test_jhtv_rss_parses_caseid_guid_pubdate(monkeypatch):
    def handler(request):
        assert str(request.url) == jhtv.RSS_URL
        return httpx.Response(200, text=_RSS)

    monkeypatch.setattr(jhtv, "_make_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = await jhtv.fetch_rss()

    assert (result.expected, result.received) == (2, 2)
    first, second = result.rows
    assert first.ids == {"jhtv_case": "C10001"}
    assert first.extra["guid"] == "guid-0001"
    assert first.published == date(2025, 10, 14)
    assert first.summary == "A small molecule that does things."
    assert first.senior_names[0]["name"] == "Ada Fictitious"
    assert first.extra["categories"] == ["Therapeutics"]
    assert second.ids == {"jhtv_case": "C10002"}
    assert second.extra["guid"] == "guid-0002"
    assert second.published == date(2025, 3, 3)
    assert second.senior_names[0]["name"] == "Bo Imaginary"
    assert {r.kind for r in result.rows} == {"jhtv"}
    assert {r.role_basis for r in result.rows} == {"jhtv"}


async def test_geo_inst_variants_query(monkeypatch):
    calls = []

    async def fake_ncbi_get(url, params):
        calls.append((url, dict(params)))
        if url.endswith("esearch.fcgi"):
            return httpx.Response(200, json={"esearchresult": {"count": "1",
                                                               "idlist": ["200111"]}})
        return httpx.Response(200, json={"result": {"uids": ["200111"], "200111": {
            "accession": "GSE111", "title": "Invented series", "summary": "Profiling.",
            "pdat": "2025/02/10", "pubmedids": ["31234567"], "gdstype": "Expression"}}})

    monkeypatch.setattr(pubmed, "_ncbi_get", fake_ncbi_get)
    result = await geo.fetch_geo(date(2025, 1, 1), date(2025, 3, 31))

    url, params = calls[0]
    assert url.endswith("esearch.fcgi") and params["db"] == "gds"
    for name in geo.INST_VARIANTS:
        assert f'"{name}"[INST]' in params["term"]
    assert "Kennedy Krieger Institute" in geo.INST_VARIANTS
    assert "gse[ETYP]" in params["term"]
    assert '("2025/01/01"[PDAT] : "2025/03/31"[PDAT])' in params["term"]
    assert calls[1][0].endswith("esummary.fcgi") and calls[1][1]["id"] == "200111"
    assert (result.expected, result.received) == (1, 1)
    assert result.rows[0].ids == {"geo_accession": "GSE111"}
    assert result.rows[0].published == date(2025, 2, 10)


async def test_odp_query_shape(monkeypatch):
    seen = []

    def handler(request):
        seen.append((request, json.loads(request.content)))
        return httpx.Response(200, json={"count": 1, "patentFileWrapperDataBag": [{
            "applicationNumberText": "18000001",
            "applicationMetaData": {
                "inventionTitle": "Invented compound",
                "filingDate": "2024-01-05", "earliestPublicationDate": "2025-05-08",
                "firstInventorName": "Ada Fictitious",
                "applicantBag": [{"applicantNameText": "The Johns Hopkins University"}],
            }}]})

    monkeypatch.setattr(odp_patents, "_make_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(odp_patents, "_api_key", lambda: "test-key")
    monkeypatch.setattr(odp_patents, "ODP_PACER", Pacer(0))
    result = await odp_patents.fetch_quarter(2025, 2)

    request, body = seen[0]
    assert request.method == "POST"
    assert request.headers["X-API-KEY"] == "test-key"
    q = body["q"]
    for field in ("applicationMetaData.applicantBag.applicantNameText",
                  "assignmentBag.assigneeBag.assigneeNameText"):
        for name in ("Johns Hopkins", "Kennedy Krieger"):
            assert f'{field}:"{name}"' in q
    assert "firstInventorName" not in q
    assert body["rangeFilters"] == [{"field": odp_patents.DATE_FIELD,
                                     "valueFrom": "2025-04-01", "valueTo": "2025-06-30"}]
    assert body["pagination"] == {"offset": 0, "limit": 100}
    assert (result.expected, result.received) == (1, 1)
    doc = result.rows[0]
    assert doc.kind == "patent" and doc.role_basis == "assignee"
    assert doc.ids == {"patent_application": "18000001"}
    assert doc.senior_names[0]["name"] == "Ada Fictitious"
    assert "abstractText" in doc.extra["missing_fields"]


def _app(*cities):
    return {"applicationNumberText": "18000002", "applicationMetaData": {
        "inventionTitle": "Invented sensor",
        "applicantBag": [{"applicantNameText": "The Johns Hopkins University"}],
        "inventorBag": [
            {"inventorNameText": f"Inventor {i}",
             "correspondenceAddressBag": [{"cityName": c}] if c else []}
            for i, c in enumerate(cities)
        ],
    }}


def test_apl_inventor_city_exclusion_when_present():
    all_laurel = odp_patents.to_document(_app("Laurel", "NORTH LAUREL"))
    assert all_laurel.raw_qualifier["apl"] is True
    assert all_laurel.raw_qualifier["inventor_cities"] == ["Laurel", "NORTH LAUREL"]

    mixed = odp_patents.to_document(_app("Laurel", "Baltimore"))
    assert mixed.raw_qualifier["apl"] is False

    no_cities = odp_patents.to_document(_app(None, None))
    assert no_cities.raw_qualifier["apl"] is False
    assert no_cities.raw_qualifier["inventor_cities"] == []

    one_unknown = odp_patents.to_document(_app("Laurel", None))
    assert one_unknown.raw_qualifier["apl"] is False


def test_datasets_never_idea_sources():
    dc = datacite.to_document({"id": "10.5061/dryad.fake1", "attributes": {
        "doi": "10.5061/dryad.fake1", "titles": [{"title": "Invented data"}],
        "descriptions": [{"description": "Measurements.", "descriptionType": "Abstract"}],
        "created": "2025-02-01T00:00:00Z",
        "relatedIdentifiers": [
            {"relatedIdentifier": "10.1000/fake.paper", "relatedIdentifierType": "DOI",
             "relationType": "IsSupplementTo"},
            {"relatedIdentifier": "31234567", "relatedIdentifierType": "PMID",
             "relationType": "IsCitedBy"},
        ]}})
    gs = geo.to_document({"accession": "GSE222", "title": "Invented series",
                          "summary": "Profiling.", "pdat": "2025/02/10",
                          "pubmedids": ["31234568"]})

    for doc in (dc, gs):
        assert doc.kind == "dataset"
        assert doc.role_basis is None
    assert dc.ids == {"datacite_doi": "10.5061/dryad.fake1"}
    assert dc.related_ids == {"doi": ["10.1000/fake.paper"], "pmid": ["31234567"]}
    assert dc.published == date(2025, 2, 1)
    assert gs.related_ids == {"pmid": ["31234568"]}
