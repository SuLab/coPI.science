"""The company_discovery job end to end (spec §7.5, §8): every request is mocked with
respx and answered from the recorded fixtures in tests/fixtures/company_discovery/, and
the Claude COI extraction (`coi_llm.extract_founder_claims`) is replaced by scripted
outcomes per PMID (`_coi` fixture), so no test calls the Anthropic API.
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
from src.services.company_sources import coi_llm, sec_form_d, wikidata
from src.services.company_sources.coi_founders import FounderClaim
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


DELFI_2024 = (
    "V.E.V. is a founder of DELFI Diagnostics, serves on the Board of Directors, and owns DELFI "
    "Diagnostics stock, which is subject to certain restrictions under university policy.")
BOTH_2021 = (
    "V.E.V. is a founder of Delfi Diagnostics and Personal Genome Diagnostics, serves on the Board of "
    "Directors and as a consultant for both organizations, and owns Delfi Diagnostics and Personal "
    "Genome Diagnostics stock, which are subject to certain restrictions under university policy.")


def _ok(pmid: str, year: int, sentence: str, *companies: tuple[str, bool]) -> coi_llm.CoiOutcome:
    return coi_llm.CoiOutcome("ok", [FounderClaim(c, "founder", pmid, year, sentence, former)
                                     for c, former in companies])


def _velculescu_script() -> dict:
    """What the extraction is scripted to return for the two recorded Velculescu
    statements (the regex parser's old yield, with PGDx flagged former)."""
    return {
        "39433569": _ok("39433569", 2024, DELFI_2024, ("DELFI Diagnostics", False)),
        "34290408": _ok("34290408", 2021, BOTH_2021,
                        ("Delfi Diagnostics", False), ("Personal Genome Diagnostics", True)),
    }


@pytest.fixture(autouse=True)
def _coi(monkeypatch):
    """Scripted `extract_founder_claims`: `script[pmid]` is a CoiOutcome or an exception
    to raise; an unscripted PMID is "skipped". `calls` lists every PMID asked."""
    state = SimpleNamespace(script=_velculescu_script(), calls=[])

    async def fake(record, pi, *, client=None):
        pmid = str(record.get("pmid"))
        state.calls.append(pmid)
        answer = state.script.get(pmid, coi_llm.CoiOutcome("skipped", [], reason="gate"))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(coi_llm, "extract_founder_claims", fake)
    return state


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


async def test_velculescu_happy_path(db_session, monkeypatch, respx_mock, _coi):
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
    assert pgdx.evidence["former"] is True and pgdx.evidence["coi"][0]["former"] is True
    assert set(pgdx.evidence["coi"][0]) == {"pmid", "year", "sentence", "former", "pi_role", "company_name", "url"}

    assert _coi.calls == ["39433569", "34290408"]  # newest first
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


async def test_no_insert_until_every_lookup_is_done(db_session, monkeypatch, respx_mock):
    """The job's one transaction holds no uncommitted pi_companies row while SEC runs."""
    _settings(monkeypatch)
    routes = _routes(respx_mock)
    user, job = await _velculescu(db_session)
    real_write = cd._write_suggestion
    seen: list[tuple[int, int, int, int]] = []

    async def spy(*args, **kwargs):
        seen.append((routes.efetch.call_count, routes.sparql.call_count,
                     routes.efts.call_count, routes.doc.call_count))
        return await real_write(*args, **kwargs)

    monkeypatch.setattr(cd, "_write_suggestion", spy)
    assert await _run(db_session, job) == "2 suggested"
    assert seen == [(1, 1, 2, 1), (1, 1, 2, 1)]


def _instant_ncbi_retries(monkeypatch) -> None:
    real_sleep = asyncio.sleep

    async def _instant(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(pubmed.asyncio, "sleep", _instant)


async def test_refused_upstream_names_are_skipped_with_a_note(db_session, monkeypatch, respx_mock):
    _settings(monkeypatch)
    _routes(respx_mock)
    user, job = await _velculescu(db_session)
    long_name = "ﷺ" * 15  # 15 characters whose NFKC form is 270
    assert len(long_name) <= 200 < len(normalize_company_name(long_name))

    async def companies(*_args):
        return [
            wikidata.WikidataCompany("Evil\x00Corp", "Q1", "https://www.wikidata.org/wiki/Q1"),
            wikidata.WikidataCompany(long_name, "Q2", "https://www.wikidata.org/wiki/Q2"),
            wikidata.WikidataCompany("Celera Corporation", "Q643341", "https://www.wikidata.org/wiki/Q643341"),
        ]

    monkeypatch.setattr(cd, "_wikidata_companies", companies)
    assert await _run(db_session, job) == (
        "3 suggested; skipped name 'Evil\\x00Corp': The company name must be one line of text.; "
        f"skipped name {long_name!r}: Company names are limited to 200 characters.")
    rows = await _rows(db_session, user.id)
    assert set(rows) == {"DELFI Diagnostics", "Personal Genome Diagnostics", "Celera Corporation"}


async def test_valid_names_skips_a_refused_coi_name_and_cleans_the_rest():
    def claim(name):
        return cd.coi_founders.FounderClaim(name, "founder", "1", 2024, "s", False)

    notes: list[str] = []
    kept = cd.valid_names([claim("Bad\x00Name"), claim("  Good Bio  "), claim("..."), claim("Bad\x00Name")], notes)
    assert [c.company_name for c in kept] == ["Good Bio"]
    assert notes == ["skipped name 'Bad\\x00Name': The company name must be one line of text."]


async def test_funding_beyond_bigint_is_dropped_and_the_suggestion_kept(db_session, monkeypatch, respx_mock):
    _settings(monkeypatch)
    _routes(respx_mock)
    user, job = await _velculescu(db_session)

    async def huge(*_args, **_kwargs):
        return sec_form_d.FundingResult(
            status="ok", funding_usd=9223372036854775808, funding_as_of=date(2022, 7, 25),
            funding_source_url="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=1")

    monkeypatch.setattr(sec_form_d, "lookup_funding", huge)
    assert await _run(db_session, job) == (
        "2 suggested; sec: funding figure out of range for DELFI Diagnostics; "
        "sec: funding figure out of range for Personal Genome Diagnostics")
    rows = await _rows(db_session, user.id)
    assert len(rows) == 2
    for row in rows.values():
        assert row.funding_usd is None and row.funding_as_of is None and row.funding_source_url is None
        assert row.evidence["form_d"]["status"] == "no_amount"
        assert row.evidence["form_d"]["funding_usd"] is None
        # Re-review: the card explains why, and no filing claims to be counted.
        assert row.evidence["form_d"]["note"] == "funding figure out of range; not recorded"
        assert not any(f["counted"] for f in row.evidence["form_d"]["filings"])


async def test_one_failed_pubmed_batch_keeps_the_others(db_session, monkeypatch, respx_mock):
    _instant_ncbi_retries(monkeypatch)
    _settings(monkeypatch)
    monkeypatch.setattr(cd, "PUBMED_BATCH", 1)

    def efetch(request):
        if request.url.params["id"] == "34290408":
            return httpx.Response(503)
        return _efetch(request)

    routes = _routes(respx_mock, efetch=efetch)
    user, job = await _velculescu(db_session)
    assert await _run(db_session, job) == "1 suggested; pubmed: partial (1 of 2 records)"
    rows = await _rows(db_session, user.id)
    assert set(rows) == {"DELFI Diagnostics"}
    assert [e["pmid"] for e in rows["DELFI Diagnostics"].evidence["coi"]] == ["39433569"]
    assert routes.efetch.call_count == 4  # the OK batch, then three tries of the 503


async def test_a_skipped_record_costs_no_call(db_session, monkeypatch, respx_mock, _coi):
    """With a cap of one, the newest record being skipped by the gate still leaves the
    one call for the next record: a skip is not counted."""
    _settings(monkeypatch)
    _routes(respx_mock)
    monkeypatch.setattr(coi_llm, "MAX_COI_CALLS_PER_PI", 1)
    del _coi.script["39433569"]
    user, job = await _velculescu(db_session)
    assert await _run(db_session, job) == "2 suggested"
    assert _coi.calls == ["39433569", "34290408"]
    rows = await _rows(db_session, user.id)
    assert set(rows) == {"Delfi Diagnostics", "Personal Genome Diagnostics"}


async def test_extraction_is_capped_at_60_disclosures(db_session, monkeypatch, respx_mock, _coi):
    """80 records, every seventh skipped: the 60 newest non-skipped are sent, the rest
    are never called, and the outcome says so."""
    _settings(monkeypatch)
    routes = _routes(respx_mock)
    assert coi_llm.MAX_COI_CALLS_PER_PI == 60
    user = User(orcid=VELCULESCU_ORCID, name="Victor Velculescu", user_role="pi")
    db_session.add(user)
    await db_session.flush()
    db_session.add(Publication(user_id=user.id, pmid="80", title="t", year=2020))
    job = Job(type="company_discovery", user_id=user.id, payload={"user_id": str(user.id), "orcid": user.orcid})
    db_session.add(job)
    await db_session.flush()

    async def records(*_args):
        return [{"pmid": str(n), "year": 2020} for n in range(1, 81)]

    monkeypatch.setattr(cd, "_fetch_coi_records", records)
    _coi.script = {str(n): coi_llm.CoiOutcome("ok", []) for n in range(1, 81) if n % 7}
    _coi.script["80"] = _ok("80", 2020, "V.E.V. is a founder of Acme Bio.", ("Acme Bio", False))
    _coi.script["1"] = _ok("1", 2020, "V.E.V. is a founder of Never Bio.", ("Never Bio", False))

    assert await _run(db_session, job) == "1 suggested; coi: capped at 60 disclosures"
    sent = [p for p in _coi.calls if int(p) % 7]
    assert len(sent) == 60 and _coi.calls == [str(n) for n in range(80, 80 - len(_coi.calls), -1)]
    assert "1" not in _coi.calls
    assert set(await _rows(db_session, user.id)) == {"Acme Bio"}
    assert routes.efetch.call_count == 0


@pytest.mark.parametrize("failure", ["unavailable", "raise"])
async def test_unavailable_disclosures_are_a_note(db_session, monkeypatch, respx_mock, _coi, failure):
    _settings(monkeypatch)
    _routes(respx_mock)
    _coi.script["34290408"] = (coi_llm.CoiOutcome("unavailable", [], reason="refusal")
                               if failure == "unavailable" else RuntimeError("boom"))
    user, job = await _velculescu(db_session)
    assert await _run(db_session, job) == "1 suggested; coi: 1 of 2 disclosures unavailable"
    rows = await _rows(db_session, user.id)
    assert set(rows) == {"DELFI Diagnostics"}
    assert rows["DELFI Diagnostics"].status == "suggested"
