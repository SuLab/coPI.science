"""The company_discovery job end to end (spec §7.5, §8): every request is mocked with
respx and answered from the recorded fixtures in tests/fixtures/company_discovery/.
Each source can fail alone and the job still completes with a per-source note; a re-run
never re-suggests a name the PI already has in any status; nothing is ever confirmed."""
import asyncio
import json
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from src.models import Job, PiCompany, Publication, User
from src.services import company_discovery as cd
from src.services import pubmed
from src.services.company_sources import sec_form_d, wikidata
from src.services.pi_companies import normalize_company_name
from src.worker.main import JobContext

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("progress_on_test_connection")]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "company_discovery"
UA = "Blackbird Labs ops@example.org"
EFETCH = f"{pubmed.EUTILS_BASE}/efetch.fcgi"
DELFI_2022 = "https://www.sec.gov/Archives/edgar/data/1783735/000178373522000002/primary_doc.xml"
SPARQL_JSON = {"content-type": "application/sparql-results+json;charset=utf-8"}
VELCULESCU_ORCID = "0000-0003-1195-438X"


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(pubmed, "_pace_interval", lambda: 0.0)
    pubmed._PACER.reset()
    for module in (wikidata, sec_form_d):
        monkeypatch.setattr(module, "_interval", lambda: 0.0)
        module._PACER.reset()

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(wikidata, "_sleep", _no_sleep)


def _settings(monkeypatch, sec_user_agent: str = UA) -> None:
    monkeypatch.setattr(cd, "get_settings", lambda: SimpleNamespace(sec_user_agent=sec_user_agent))


def _article(pmid: str) -> str:
    text = (FIXTURES / f"pubmed_{pmid}.xml").read_text(encoding="utf-8")
    return re.search(r"<PubmedArticle>.*</PubmedArticle>", text, re.S).group(0)


def _efetch(request: httpx.Request) -> httpx.Response:
    ids = request.url.params["id"].split(",")
    body = "".join(_article(p) for p in ids if (FIXTURES / f"pubmed_{p}.xml").exists())
    return httpx.Response(200, text=f'<?xml version="1.0" ?>\n<PubmedArticleSet>{body}</PubmedArticleSet>')


def _efts(request: httpx.Request) -> httpx.Response:
    if request.url.params["q"] == '"DELFI Diagnostics"':
        data = json.loads((FIXTURES / "efts_delfi_diagnostics.json").read_text())
        data["hits"]["hits"] = [h for h in data["hits"]["hits"] if h["_source"]["adsh"] == "0001783735-22-000002"]
        return httpx.Response(200, json=data)
    return httpx.Response(200, text=(FIXTURES / "efts_no_hits.json").read_text())


def _wikidata_answer(name: str = "wikidata_no_p112_0000-0003-1195-438X.json") -> httpx.Response:
    return httpx.Response(200, content=(FIXTURES / name).read_bytes(), headers=SPARQL_JSON)


def _routes(respx_mock, *, efetch=None, sparql=None, efts=None):
    """Every upstream the job touches; a test overrides one to make it fail."""
    return SimpleNamespace(
        efetch=respx_mock.get(EFETCH).mock(side_effect=efetch or _efetch),
        sparql=respx_mock.get(wikidata.SPARQL_URL).mock(side_effect=sparql or [_wikidata_answer()]),
        efts=respx_mock.get(sec_form_d.EFTS_URL).mock(side_effect=efts or _efts),
        doc=respx_mock.get(DELFI_2022).mock(return_value=httpx.Response(
            200, text=(FIXTURES / "form_d_delfi_000178373522000002.xml").read_text(encoding="utf-8"))),
    )


async def _velculescu(db_session) -> tuple[User, Job]:
    user = User(orcid=VELCULESCU_ORCID, name="Victor Velculescu", user_role="pi")
    db_session.add(user)
    await db_session.flush()
    db_session.add(Publication(user_id=user.id, pmid="39433569", title="DELFI CRC paper", year=2024))
    db_session.add(Publication(user_id=user.id, pmid="34290408", title="MANAFEST paper", year=2021))
    job = Job(type="company_discovery", user_id=user.id, payload={"user_id": str(user.id), "orcid": user.orcid})
    db_session.add(job)
    await db_session.flush()
    return user, job


def _ctx(job: Job) -> JobContext:
    return JobContext(id=job.id, type=job.type, user_id=job.user_id, payload=dict(job.payload),
                      attempts=job.attempts or 0, max_attempts=job.max_attempts or 3)


async def _run(db_session, job: Job) -> str:
    await cd.execute_company_discovery(_ctx(job), db_session)
    await db_session.refresh(job)
    last = job.payload["progress"][-1]
    assert last["step"] == cd.DISCOVERY_DONE_STEP
    return last["detail"]


async def _rows(db_session, user_id) -> dict[str, PiCompany]:
    rows = (await db_session.execute(select(PiCompany).where(PiCompany.user_id == user_id))).scalars().all()
    return {r.company_name: r for r in rows}


async def test_velculescu_happy_path(db_session, monkeypatch, respx_mock):
    _settings(monkeypatch)
    routes = _routes(respx_mock)
    user, job = await _velculescu(db_session)

    assert await _run(db_session, job) == "2 suggested"

    rows = await _rows(db_session, user.id)
    assert set(rows) == {"DELFI Diagnostics", "Personal Genome Diagnostics"}
    delfi = rows["DELFI Diagnostics"]
    assert delfi.normalized_name == normalize_company_name("Delfi Diagnostics, Inc.")
    assert (delfi.status, delfi.origin, delfi.pi_role) == ("suggested", "discovered", "founder")
    assert delfi.created_by_user_id is None and delfi.reviewed_by_user_id is None
    assert delfi.source_url == "https://pubmed.ncbi.nlm.nih.gov/39433569/"
    assert delfi.funding_usd == 224_999_876 and delfi.funding_as_of == date(2022, 7, 25)
    assert delfi.funding_source_url == (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=0001783735"
        "&type=D&dateb=&owner=include&count=40"
    )
    assert [e["pmid"] for e in delfi.evidence["coi"]] == ["39433569", "34290408"]
    assert delfi.evidence["coi"][0]["sentence"].startswith("V.E.V. is a founder of DELFI Diagnostics")
    assert delfi.evidence["former"] is False and delfi.evidence["wikidata"] == []
    (filing,) = delfi.evidence["form_d"]["filings"]
    assert filing["accession"] == "0001783735-22-000002" and filing["pi_listed"] is True
    assert filing["pi_relationships"] == ["Executive Officer", "Director", "Promoter"]

    pgdx = rows["Personal Genome Diagnostics"]
    assert pgdx.source_url == "https://pubmed.ncbi.nlm.nih.gov/34290408/"
    assert pgdx.funding_usd is None and pgdx.funding_as_of is None and pgdx.funding_source_url is None
    assert pgdx.evidence["form_d"]["status"] == "no_filings"

    assert routes.efetch.call_count == 1 and routes.sparql.call_count == 1
    assert routes.efts.call_count == 2 and routes.doc.call_count == 1
    assert routes.efts.calls[0].request.headers["User-Agent"] == UA


async def test_a_rerun_skips_names_in_any_status(db_session, monkeypatch, respx_mock):
    _settings(monkeypatch)
    routes = _routes(respx_mock)
    user, job = await _velculescu(db_session)
    db_session.add(PiCompany(
        user_id=user.id, company_name="Delfi Diagnostics, Inc.",
        normalized_name=normalize_company_name("Delfi Diagnostics, Inc."), pi_role="founder",
        source_url="https://example.org/delfi", status="confirmed", origin="manual"))
    db_session.add(PiCompany(
        user_id=user.id, company_name="Personal Genome Diagnostics",
        normalized_name=normalize_company_name("Personal Genome Diagnostics"), pi_role="founder",
        source_url="https://example.org/pgdx", status="rejected", origin="discovered"))
    await db_session.flush()

    assert await _run(db_session, job) == "0 suggested (2 already listed)"
    rows = await _rows(db_session, user.id)
    assert {r.status for r in rows.values()} == {"confirmed", "rejected"}
    assert routes.efts.call_count == 0  # no funding lookup for a name already listed


async def test_a_second_run_suggests_nothing_new(db_session, monkeypatch, respx_mock):
    _settings(monkeypatch)
    _routes(respx_mock, sparql=[_wikidata_answer(), _wikidata_answer()])
    user, job = await _velculescu(db_session)
    assert await _run(db_session, job) == "2 suggested"
    # The worker loop, not the handler, completes a job; until then the
    # one-active-per-PI index refuses a second row.
    job.status = "completed"
    await db_session.flush()
    second = Job(type="company_discovery", user_id=user.id, payload=dict(job.payload))
    db_session.add(second)
    await db_session.flush()
    assert await _run(db_session, second) == "0 suggested (2 already listed)"
    assert len(await _rows(db_session, user.id)) == 2


async def test_ncbi_failure_alone_is_a_note(db_session, monkeypatch, respx_mock):
    real_sleep = asyncio.sleep

    async def _instant(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(pubmed.asyncio, "sleep", _instant)
    _settings(monkeypatch)
    _routes(respx_mock, efetch=lambda request: httpx.Response(503))
    user, job = await _velculescu(db_session)
    assert await _run(db_session, job) == "0 suggested; pubmed: lookup unavailable"
    assert await _rows(db_session, user.id) == {}


async def test_ncbi_failure_keeps_wikidata_suggestions(db_session, monkeypatch, respx_mock):
    """A PI whose only founder source is Wikidata: the row cites the Wikidata item."""
    real_sleep = asyncio.sleep

    async def _instant(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(pubmed.asyncio, "sleep", _instant)
    _settings(monkeypatch)
    _routes(respx_mock, efetch=lambda request: httpx.Response(503),
            sparql=[_wikidata_answer("wikidata_p112_0000-0002-7086-765X.json")])
    user = User(orcid="0000-0002-7086-765X", name="Craig Venter", user_role="pi")
    db_session.add(user)
    await db_session.flush()
    db_session.add(Publication(user_id=user.id, pmid="39433569", title="t", year=2024))
    job = Job(type="company_discovery", user_id=user.id, payload={"user_id": str(user.id), "orcid": user.orcid})
    db_session.add(job)
    await db_session.flush()

    assert await _run(db_session, job) == "3 suggested; pubmed: lookup unavailable"
    rows = await _rows(db_session, user.id)
    celera = rows["Celera Corporation"]
    assert celera.source_url == "https://www.wikidata.org/wiki/Q643341"
    assert celera.pi_role == "founder" and celera.evidence["coi"] == []
    assert celera.evidence["wikidata"] == [
        {"item": "Q643341", "url": "https://www.wikidata.org/wiki/Q643341", "label": "Celera Corporation"}]


async def test_wikidata_429_alone_is_a_note(db_session, monkeypatch, respx_mock):
    _settings(monkeypatch)
    _routes(respx_mock, sparql=[httpx.Response(429, headers={"Retry-After": "1"}),
                                httpx.Response(429, headers={"Retry-After": "1"})])
    user, job = await _velculescu(db_session)
    assert await _run(db_session, job) == "2 suggested; wikidata: lookup unavailable"
    assert len(await _rows(db_session, user.id)) == 2


@pytest.mark.parametrize("failure", ["429", "timeout"])
async def test_sec_failure_alone_is_a_note(db_session, monkeypatch, respx_mock, failure):
    _settings(monkeypatch)

    def efts(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(429)

    routes = _routes(respx_mock, efts=efts)
    user, job = await _velculescu(db_session)
    assert await _run(db_session, job) == "2 suggested; sec: funding lookup unavailable"
    rows = await _rows(db_session, user.id)
    for row in rows.values():
        assert row.funding_usd is None and row.funding_source_url is None
        assert row.evidence["form_d"]["status"] == "unavailable"
        assert row.evidence["form_d"]["note"] == "funding lookup unavailable"
    assert routes.doc.call_count == 0


async def test_unset_sec_user_agent_skips_sec_and_wikidata(db_session, monkeypatch, respx_mock):
    _settings(monkeypatch, sec_user_agent="")
    routes = _routes(respx_mock)
    user, job = await _velculescu(db_session)
    assert await _run(db_session, job) == (
        "2 suggested; wikidata: lookup unavailable; sec: funding lookup unavailable")
    assert routes.efts.call_count == 0 and routes.sparql.call_count == 0
    rows = await _rows(db_session, user.id)
    assert {r.evidence["form_d"]["reason"] for r in rows.values()} == {"SEC_USER_AGENT unset"}


async def test_discovery_never_writes_a_confirmed_row(db_session, monkeypatch, respx_mock):
    _settings(monkeypatch)
    _routes(respx_mock)
    user, job = await _velculescu(db_session)
    await _run(db_session, job)
    rows = (await db_session.execute(select(PiCompany).where(PiCompany.user_id == user.id))).scalars().all()
    assert rows and all(r.status == "suggested" and r.origin == "discovered" for r in rows)


async def test_a_database_error_fails_the_job(db_session, monkeypatch, respx_mock):
    _settings(monkeypatch)
    _routes(respx_mock)
    _user, job = await _velculescu(db_session)

    async def broken(*_a, **_k):
        raise SQLAlchemyError("connection lost")

    monkeypatch.setattr(cd, "_write_suggestion", broken)
    with pytest.raises(SQLAlchemyError):
        await cd.execute_company_discovery(_ctx(job), db_session)


async def test_a_deleted_account_completes_with_a_note(db_session):
    job = Job(type="company_discovery", user_id=None, payload={"user_id": "00000000-0000-0000-0000-000000000000"})
    db_session.add(job)
    await db_session.flush()
    assert await _run(db_session, job) == "0 suggested; the account no longer exists"


async def test_merge_joins_spellings_and_keeps_the_newest_statement_first():
    def claim(name, pmid, year, role="founder"):
        return cd.coi_founders.FounderClaim(name, role, pmid, year, f"{name} sentence", False)

    merged = cd.merge_candidates(
        [claim("Delfi Diagnostics", "34290408", 2021), claim("DELFI Diagnostics", "39433569", 2024)],
        [wikidata.WikidataCompany("Delfi Diagnostics, Inc.", "Q1", "https://www.wikidata.org/wiki/Q1"),
         wikidata.WikidataCompany("Celera Corporation", "Q643341", "https://www.wikidata.org/wiki/Q643341")],
    )
    delfi = merged[normalize_company_name("DELFI Diagnostics")]
    assert delfi.company_name == "DELFI Diagnostics"
    assert [c.pmid for c in delfi.coi_claims] == ["39433569", "34290408"]
    assert [w.item for w in delfi.wikidata_items] == ["Q1"]
    assert delfi.source_url == "https://pubmed.ncbi.nlm.nih.gov/39433569/"
    celera = merged[normalize_company_name("Celera Corporation")]
    assert celera.pi_role == "founder" and celera.source_url == "https://www.wikidata.org/wiki/Q643341"


@pytest.mark.parametrize(("args", "line"), [
    ((2, 0, []), "2 suggested"),
    ((0, 2, []), "0 suggested (2 already listed)"),
    ((2, 0, ["sec: funding lookup unavailable"]), "2 suggested; sec: funding lookup unavailable"),
])
async def test_outcome_line(args, line):
    assert cd.outcome_line(*args) == line
