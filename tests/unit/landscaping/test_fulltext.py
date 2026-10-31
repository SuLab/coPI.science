"""Full-text retrieval (plan Task 5.1, spec 2026-10-30 §6.2, D6, D41).
No network: HTTP runs over an httpx.MockTransport on recorded-shape fixtures with invented
article content."""
import json

import httpx
import pytest

from src.landscaping import fulltext
from src.models.landscape import LandscapeDocument
from src.services import http_pacing

PMCID = "PMC9000001"
LISTING = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
    f"<Contents><Key>metadata/{PMCID}.1.json</Key></Contents>"
    f"<Contents><Key>metadata/{PMCID}.2.json</Key></Contents></ListBucketResult>"
)
JATS = """<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <front><article-meta>
    <title-group>
      <article-title>Invented kinase blockade in a model disease</article-title>
    </title-group>
    <abstract><p>We describe an invented inhibitor of an invented kinase.</p></abstract>
  </article-meta></front>
  <body>
    <sec sec-type="intro"><title>Introduction</title><p>Background on the invented kinase.</p></sec>
    <sec><title>Results</title><p>The inhibitor reduced signal <xref ref-type="bibr">[1]</xref>.</p>
      <fig id="f1"><label>Figure 1</label>
        <caption><p>Dose response of the inhibitor.</p></caption></fig>
    </sec>
    <sec><title>Discussion</title><p>The invented kinase is tractable.</p></sec>
    <sec sec-type="materials|methods"><title>Materials and Methods</title>
      <p>Cells were cultured.</p></sec>
    <sec><title>References</title><p>Body reference section text.</p></sec>
  </body>
  <back><ref-list><ref id="r1"><element-citation><article-title>Cited invented paper</article-title>
  </element-citation></ref></ref-list></back>
</article>"""


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    """Retries and paced starts do not sleep; each pacer starts with an empty schedule."""
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(http_pacing, "_sleep", no_sleep)
    for pacer in (fulltext._BIOC_PACER, fulltext._EUROPEPMC_PACER, fulltext._S3_PACER,
                  fulltext._BIORXIV_PACER):
        pacer.reset()
        monkeypatch.setattr(pacer, "_interval", 0.0)


def _doc(**overrides) -> LandscapeDocument:
    values = {
        "kind": "work", "canonical_external_id": "10.9999/invented.1", "doi": "10.9999/invented.1",
        "pmcid": PMCID, "title": "Invented title", "summary_text": "Invented summary.",
        "is_retracted": False, "s1_status": "eligible",
    }
    values.update(overrides)
    return LandscapeDocument(**values)


def _meta(license_code="CC BY", *, manuscript=False, retracted=False) -> dict:
    return {
        "pmcid": PMCID, "version": 2, "license_code": license_code,
        "is_manuscript": manuscript, "is_retracted": retracted, "is_pmc_openaccess": True,
        "xml_url": f"s3://pmc-oa-opendata/{PMCID}.2/{PMCID}.2.xml?md5=0000",
    }


def _install(monkeypatch, handler) -> list[httpx.Request]:
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(fulltext, "_make_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(recording)))
    return requests


def _pmc_handler(meta: dict, jats: str = JATS):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "pmc-oa-opendata.s3.amazonaws.com":
            if path == "/":
                return httpx.Response(200, text=LISTING)
            if path == f"/metadata/{PMCID}.2.json":
                return httpx.Response(200, json=meta)
            if path == f"/{PMCID}.2/{PMCID}.2.xml":
                return httpx.Response(200, text=jats)
        return httpx.Response(404)
    return handler


def _text_fetches(requests: list[httpx.Request]) -> list[httpx.Request]:
    return [r for r in requests if r.url.path.endswith((".xml", "/fullTextXML", "/unicode"))]


async def test_cc_by_nc_is_summary_only(monkeypatch):
    requests = _install(monkeypatch, _pmc_handler(_meta("CC BY-NC")))
    result = await fulltext.fetch_full_text(_doc())
    assert result.basis == "summary"
    assert result.license == "CC BY-NC"
    assert result.route is None
    assert "Invented summary." in result.text
    assert not _text_fetches(requests)
    assert all(r.url.host == "pmc-oa-opendata.s3.amazonaws.com" for r in requests)

    def europepmc(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"resultList": {"result": [{
                "pmcid": "PMC9000002", "license": "cc by-nc", "isOpenAccess": "Y",
                "inEPMC": "Y"}]}})
        return httpx.Response(404)

    requests = _install(monkeypatch, europepmc)
    result = await fulltext.fetch_full_text(_doc(pmcid=None))
    assert (result.basis, result.license) == ("summary", "CC BY-NC")
    assert not _text_fetches(requests)


async def test_author_manuscript_is_summary_only(monkeypatch):
    requests = _install(monkeypatch, _pmc_handler(_meta("CC BY", manuscript=True)))
    result = await fulltext.fetch_full_text(_doc())
    assert result.basis == "summary"
    assert not _text_fetches(requests)
    assert all(r.url.host == "pmc-oa-opendata.s3.amazonaws.com" for r in requests)


async def test_retracted_skipped(monkeypatch):
    requests = _install(monkeypatch, _pmc_handler(_meta("CC BY", retracted=True)))
    result = await fulltext.fetch_full_text(_doc())
    assert result.basis == "summary"
    assert not _text_fetches(requests)

    requests = _install(monkeypatch, _pmc_handler(_meta("CC BY")))
    result = await fulltext.fetch_full_text(_doc(is_retracted=True))
    assert result.basis == "summary"
    assert requests == []


async def test_transient_error_is_fetch_failed_not_summary(monkeypatch):
    requests = _install(monkeypatch, lambda request: httpx.Response(503))
    result = await fulltext.fetch_full_text(_doc())
    assert result.basis == "fetch_failed"
    assert result.route is None
    listing_calls = [r for r in requests if r.url.host == "pmc-oa-opendata.s3.amazonaws.com"]
    assert len(listing_calls) == 4  # the first try and 3 retries (D41)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("invented timeout", request=request)

    _install(monkeypatch, timeout)
    assert (await fulltext.fetch_full_text(_doc())).basis == "fetch_failed"

    def text_fails(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith((".xml", "/unicode")):
            return httpx.Response(502)
        return _pmc_handler(_meta("CC BY"))(request)

    _install(monkeypatch, text_fails)
    result = await fulltext.fetch_full_text(_doc(doi=None))
    assert result.basis == "fetch_failed"


async def test_references_dropped(monkeypatch):
    pairs = fulltext.sections_from_jats(JATS)
    labels = [label for label, _ in pairs]
    assert labels == ["title", "abstract", "introduction", "results", "discussion", "methods",
                      "captions"]
    joined = "\n".join(text for _, text in pairs)
    assert "Cited invented paper" not in joined
    assert "Body reference section text." not in joined
    assert dict(pairs)["captions"] == "Figure 1 Dose response of the inhibitor."
    assert "Dose response" not in dict(pairs)["results"]

    _install(monkeypatch, _pmc_handler(_meta("CC BY")))
    result = await fulltext.fetch_full_text(_doc())
    assert (result.basis, result.route, result.license) == ("full_text", "pmc", "CC BY")
    assert "Cited invented paper" not in result.text
    for label, start, end in result.sections:
        assert result.text[start:end] == dict(pairs)[label]


async def test_truncation_at_section_boundary(monkeypatch):
    def sec(title: str, marker: str) -> str:
        return f"<sec><title>{title}</title><p>{marker} " + "word " * 14_000 + "</p></sec>"

    long_jats = (
        "<article><front><article-meta><title-group><article-title>Invented long article"
        "</article-title></title-group></article-meta></front><body>"
        + sec("Introduction", "MARKONE") + sec("Results", "MARKTWO")
        + sec("Discussion", "MARKTHREE") + sec("Methods", "MARKFOUR")
        + "</body></article>"
    )
    _install(monkeypatch, _pmc_handler(_meta("CC BY"), long_jats))
    result = await fulltext.fetch_full_text(_doc())
    assert result.basis == "full_text"
    assert result.truncated is True
    assert len(result.text) <= fulltext.MAX_CHARS
    assert [label for label, _, _ in result.sections] == ["title", "introduction", "results"]
    assert result.sections[-1][2] == len(result.text)
    assert result.text.rstrip().endswith("word")
    assert "MARKTWO" in result.text
    assert "MARKTHREE" not in result.text
    json.dumps(result.sections)  # the index is stored as JSONB
