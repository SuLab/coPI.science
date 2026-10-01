"""DP-06: at most N ESearch + ceil(N/100) EFetch requests for N unmapped DOIs, over one
client per ncbi_session; a per-item error in a batch falls back to single fetches so
one bad record loses only its own DOI (Review Focus 3); the shared systemic-run
counter still stops a run of identical failures; and convert_dois_to_pmids and
resolve_corpus return exactly what the base code returned (Freeze B24)."""
import dataclasses
import math

import httpx
import pytest

from src.services import corpus, pubmed
from tests.unit import _frozen_doi_resolution as frozen

pytestmark = pytest.mark.asyncio


def _xml(records):
    arts = "".join(
        f"<PubmedArticle><MedlineCitation><PMID>{p}</PMID><Article><ArticleTitle>T{p}</ArticleTitle>"
        f"<Journal><JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue><Title>J</Title></Journal>"
        f"<AuthorList><Author><LastName>Green</LastName><ForeName>Rachel</ForeName>"
        f"<AffiliationInfo><Affiliation>Johns Hopkins University</Affiliation></AffiliationInfo></Author></AuthorList>"
        f"<PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>"
        f"</Article></MedlineCitation><PubmedData><ArticleIdList><ArticleId IdType=\"doi\">{d}</ArticleId>"
        f"</ArticleIdList></PubmedData></PubmedArticle>"
        for p, d in records
    )
    return f"<?xml version='1.0'?><PubmedArticleSet>{arts}</PubmedArticleSet>"


class _Recorder:
    def __init__(self, bad_pmid=None):
        self.esearch = self.efetch = self.idconv = 0
        self.bad = bad_pmid
        self.clients = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "idconv" in url:
            self.idconv += 1
            return httpx.Response(200, json={"records": []})
        if "esearch" in url:
            self.esearch += 1
            doi = request.url.params["term"].removesuffix("[doi]")
            n = int(doi.rsplit(".", 1)[-1])
            return httpx.Response(200, json={"esearchresult": {"idlist": [str(1000 + n)]}})
        if "efetch" in url:
            self.efetch += 1
            ids = request.url.params["id"].split(",")
            if self.bad and self.bad in ids:
                return httpx.Response(400, text="bad id")
            return httpx.Response(200, text=_xml([(p, f"10.1000/x.{int(p) - 1000}") for p in ids]))
        return httpx.Response(404)


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(pubmed, "_pace_interval", lambda: 0.0)
    pubmed._PACER.reset()

    def make():
        rec.clients += 1
        return httpx.AsyncClient(transport=httpx.MockTransport(rec.handler))

    monkeypatch.setattr(pubmed, "_make_client", make)
    return rec


async def test_request_counts_and_one_client(recorder):
    dois = [f"10.1000/x.{i}" for i in range(150)]
    async with pubmed.ncbi_session():
        mapping = await pubmed.convert_dois_to_pmids(dois, strict=True)
    assert list(mapping.items()) == [(d, str(1000 + i)) for i, d in enumerate(dois)]
    assert recorder.esearch <= 150
    assert recorder.efetch <= math.ceil(150 / 100)
    assert recorder.clients == 1


async def test_batched_verification_per_item_error_loses_only_that_doi(recorder):
    recorder.bad = "1007"
    dropped: list[str] = []
    mapping = await pubmed.convert_dois_to_pmids(
        [f"10.1000/x.{i}" for i in range(10)], strict=True, permanently_dropped=dropped
    )
    assert "10.1000/x.7" not in mapping and len(mapping) == 9
    assert dropped == ["10.1000/x.7"]


async def test_a_round_trip_mismatch_is_a_miss(recorder, monkeypatch):
    def handler(request):
        if "esearch" in str(request.url):
            return httpx.Response(200, json={"esearchresult": {"idlist": ["1001"]}})
        return httpx.Response(200, text=_xml([("1001", "10.1000/other")]))

    monkeypatch.setattr(pubmed, "_make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await pubmed.convert_dois_to_pmids(["10.1000/x.1"], strict=True) == {}


async def test_alternating_esearch_failures_and_hits_do_not_trip_the_systemic_run(recorder, monkeypatch):
    """A good single-hit ESearch answer ends a failure run (as the per-DOI loop's
    verified hit did), so F, hit, F, hit, F is three isolated drops, not systemic."""
    inner = recorder.handler

    def handler(request):
        if "esearch" in str(request.url):
            n = int(request.url.params["term"].removesuffix("[doi]").rsplit(".", 1)[-1])
            if n % 2 == 0:
                return httpx.Response(400, text="bad term")
        return inner(request)

    monkeypatch.setattr(pubmed, "_make_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    dropped: list[str] = []
    mapping = await pubmed.convert_dois_to_pmids(
        [f"10.1000/x.{i}" for i in range(5)], strict=True, permanently_dropped=dropped
    )
    assert mapping == {"10.1000/x.1": "1001", "10.1000/x.3": "1003"}
    assert dropped == ["10.1000/x.0", "10.1000/x.2", "10.1000/x.4"]


# --- Differential equality against the base resolver (Tasks 14/15, Freeze B24) ---
#
# The stored publication rows, their insertion order and so the exported profile
# (the lab bot's system prompt) follow ``CorpusResult``. Each test below runs the
# new code and ``tests/unit/_frozen_doi_resolution.py`` (the base 9b66cc85 copies)
# over the same synthetic NCBI world and requires identical results, list and
# dict ORDER included. Only ORCID and OpenAlex are faked at their use site; every
# NCBI request goes through the MockTransport below.

_PI_AFF = "Johns Hopkins University School of Medicine, Baltimore, MD"
_KINDS = {
    "full_aff": ("Green", "Rachel", "R", [_PI_AFF]),
    "full_noaff": ("Green", "Rachel", "R", []),
    "full_other": ("Green", "Rachel", "R", ["University of Oxford, Oxford, UK"]),
    "bare_noaff": ("Green", "R", "R", []),
    "bare_aff": ("Green", "R", "R", [_PI_AFF]),
    "other": ("Smith", "John", "J", ["University of Oxford, Oxford, UK"]),
}


def _paper_xml(pmid, doi, title, year, kind, pub_type):
    # A Hopkins co-author on every paper: their affiliation must never count for the PI.
    authors = ['<Author><LastName>Lee</LastName><ForeName>Ann</ForeName><Initials>A</Initials>'
               f'<AffiliationInfo><Affiliation>{_PI_AFF}</Affiliation></AffiliationInfo></Author>']
    if kind == "consortium":
        authors.append("<Author><CollectiveName>The Big Consortium</CollectiveName></Author>")
    else:
        last, fore, initials, affs = _KINDS[kind]
        aff_xml = "".join(f"<AffiliationInfo><Affiliation>{a}</Affiliation></AffiliationInfo>" for a in affs)
        authors.append(f"<Author><LastName>{last}</LastName><ForeName>{fore}</ForeName>"
                       f"<Initials>{initials}</Initials>{aff_xml}</Author>")
    ids = f'<ArticleId IdType="doi">{doi}</ArticleId>' if doi else ""
    return (
        f"<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article>"
        f"<Journal><JournalIssue><PubDate><Year>{year}</Year></PubDate></JournalIssue><Title>J{pmid}</Title></Journal>"
        f"<ArticleTitle>{title}</ArticleTitle><Abstract><AbstractText>A{pmid}</AbstractText></Abstract>"
        f"<AuthorList>{''.join(authors)}</AuthorList>"
        f"<PublicationTypeList><PublicationType>{pub_type}</PublicationType></PublicationTypeList>"
        f"</Article></MedlineCitation><PubmedData><ArticleIdList>{ids}</ArticleIdList></PubmedData>"
        f"</PubmedArticle>"
    )


class _World:
    """A deterministic NCBI: ``papers`` maps pmid -> (doi, title, year, kind,
    pub_type); ``served_as`` makes EFetch return another PMID's record for a
    requested one; ``bad_pmids`` 400 any EFetch that names them and ``bad_dois``
    400 their ESearch (per-item failures); ``reverse`` answers EFetch in reverse
    request order."""

    def __init__(self, papers, *, orcid_works=(), openalex_works=(), s3=(), s4=(), idconv=None,
                 doi_hits=None, served_as=None, bad_pmids=(), bad_dois=(), reverse=False):
        self.papers = papers
        self.orcid_works, self.openalex_works = list(orcid_works), list(openalex_works)
        self.s3, self.s4 = list(s3), list(s4)
        self.idconv, self.doi_hits = dict(idconv or {}), dict(doi_hits or {})
        self.served_as = dict(served_as or {})
        self.bad_pmids, self.bad_dois = set(bad_pmids), set(bad_dois)
        self.reverse = reverse

    def handler(self, request: httpx.Request) -> httpx.Response:
        url, params = str(request.url), request.url.params
        if "idconv" in url:
            recs = [{"doi": d.lower(), "pmid": self.idconv[d]} if d in self.idconv
                    else {"doi": d, "status": "error"} for d in params["ids"].split(",")]
            return httpx.Response(200, json={"records": recs})
        if "esearch" in url:
            term = params["term"]
            if term.endswith("[doi]"):
                doi = term.removesuffix("[doi]")
                if doi in self.bad_dois:
                    return httpx.Response(400, text="bad term")
                ids = self.doi_hits.get(doi, [])
            else:
                ids = self.s3 if "[auid]" in term else self.s4
            return httpx.Response(200, json={"esearchresult": {"idlist": list(ids)}})
        if "efetch" in url:
            ids = params["id"].split(",")
            if self.bad_pmids & set(ids):
                return httpx.Response(400, text="bad id")
            served = [self.served_as.get(p, p) for p in ids]
            served = [p for p in served if p in self.papers]
            if self.reverse:
                served.reverse()
            return httpx.Response(200, text="<?xml version='1.0'?><PubmedArticleSet>"
                                  + "".join(_paper_xml(p, *self.papers[p]) for p in served)
                                  + "</PubmedArticleSet>")
        return httpx.Response(404)


def _install(monkeypatch, world: _World) -> None:
    monkeypatch.setattr(pubmed, "_pace_interval", lambda: 0.0)
    pubmed._PACER.reset()
    monkeypatch.setattr(pubmed, "_make_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(world.handler)))

    async def orcid_works(orcid, *, strict=False):
        return [dict(w) for w in world.orcid_works]

    async def openalex_works(orcid):
        return [dict(w) for w in world.openalex_works]

    for mod in (corpus, frozen):
        monkeypatch.setattr(mod, "fetch_orcid_works", orcid_works)
        monkeypatch.setattr(mod, "fetch_works_by_orcid", openalex_works)


def _ordered(value):
    """``value`` with every dict turned into its item list, so == also compares order."""
    if isinstance(value, dict):
        return [(k, _ordered(v)) for k, v in value.items()]
    if isinstance(value, (list, tuple)):
        return [_ordered(v) for v in value]
    return value


async def _both_resolve(monkeypatch, world):
    """(new, frozen) outcome of resolve_corpus: a CorpusResult or the raised error."""
    _install(monkeypatch, world)
    outcomes = []
    for resolve in (corpus.resolve_corpus, frozen.resolve_corpus):
        try:
            outcomes.append(await resolve("0000-0001-2345-6789", "Rachel Green", "Johns Hopkins University"))
        except corpus.CorpusStageError as exc:
            outcomes.append(exc)
    return outcomes


def _assert_same_result(new, old):
    assert isinstance(new, corpus.CorpusResult) and isinstance(old, corpus.CorpusResult), (new, old)
    for f in dataclasses.fields(corpus.CorpusResult):
        assert _ordered(getattr(new, f.name)) == _ordered(getattr(old, f.name)), f.name


def _mixed_world(**overrides) -> _World:
    """More than 100 stage PMIDs (two EFetch chunks, the cap of 50 applies), S1
    PMID works and DOI-only works (idconv with an uppercase DOI, single-hit
    verified, multi-hit, no hit, round-trip mismatch, a DOI-resolved PMID another
    stage also proposes), S2 PMID and DOI-only works, S3 and S4 in mixed orders,
    S4-only candidates of every gate outcome, year ties and duplicate titles."""
    papers: dict[str, tuple] = {}

    def add(pmid, *, doi=None, year=None, kind="full_aff", title=None, pub_type="Journal Article"):
        p = str(pmid)
        papers[p] = (doi or f"10.7000/p{p}", title or f"Paper {p}", year or 2000 + pmid % 7, kind, pub_type)
        return p

    s1_pmids = [add(n) for n in range(101, 111)]
    for p, doi in zip((201, 202, 203, 204, 205, 206), ("10.5555/IDCONV.1", "10.5555/ES.1", "x", "y",
                                                         "10.5555/other", "10.5555/es.2"), strict=True):
        add(p, doi=doi)
    add(207, doi="10.5555/es.3", year=2003)
    add(208, doi="10.5555/es.4", year=2003, kind="bare_noaff")  # S2-only: unconfirmed initial
    add(111)
    add(112, kind="bare_noaff")
    s3 = sorted(range(120, 200), key=lambda n: (n * 37) % 80)
    for n in s3:
        add(n)
    add(160, kind="bare_noaff")         # anchored by S3: kept
    add(161, kind="consortium")
    add(162, pub_type="Published Erratum")
    add(163, kind="other")
    add(170, title="Shared title", year=2010)
    add(171, title="Shared title", year=2010)
    add(172, title="Another shared title", year=2011)
    add(173, title="Another shared title", year=2012)
    add(301, kind="full_aff")
    add(302, kind="full_other")         # S4-only, affiliation present and wrong: flagged
    add(303, kind="full_noaff")         # S4-only, no affiliation: kept (institution searched)
    add(304, kind="bare_noaff")
    add(305, kind="other")
    add(306, kind="bare_aff")
    s3_ids = [str(n) for n in s3] + ["206", "103"]
    s4_ids = ["305", "150", "301", "304", "151", "302", "306", "303", "152"]
    orcid_works = [{"pmid": p, "doi": f"10.7000/p{p}"} for p in s1_pmids] + [
        {"pmid": None, "doi": d} for d in ("10.5555/IDCONV.1", "10.5555/es.1", "10.5555/es.multi",
                                           "10.5555/es.none", "10.5555/es.mismatch", "10.5555/es.2")]
    openalex_works = [{"pmid": "101"}, {"pmid": "111"}, {"pmid": "112"},
                      {"pmid": None, "doi": "10.5555/es.1"}, {"pmid": None, "doi": "10.5555/es.3"},
                      {"pmid": None, "doi": "10.5555/es.4"}]
    kwargs = dict(
        orcid_works=orcid_works, openalex_works=openalex_works, s3=s3_ids, s4=s4_ids,
        idconv={"10.5555/IDCONV.1": "201"},
        doi_hits={"10.5555/es.1": ["202"], "10.5555/es.multi": ["203", "204"],
                  "10.5555/es.mismatch": ["205"], "10.5555/es.2": ["206"],
                  "10.5555/es.3": ["207"], "10.5555/es.4": ["208"]},
    )
    kwargs.update(overrides)
    return _World(papers, **kwargs)


@pytest.mark.parametrize("reverse", [False, True], ids=["request-order", "reversed-efetch"])
async def test_resolve_corpus_equals_todays_on_fixtures(monkeypatch, reverse):
    new, old = await _both_resolve(monkeypatch, _mixed_world(reverse=reverse))
    _assert_same_result(new, old)
    assert len(old.ranked) > 50 and len(old.kept) == 50 and old.flagged


def _redirect_world() -> _World:
    """Records that come back under another PMID: a two-candidate verification batch
    where PMID 402 is served as 499 (Task 14 correction 2), an S3 PMID served as one
    S4 also proposes (a duplicate record), and one served as a PMID no stage has."""
    papers = {str(p): (f"10.7000/p{p}", f"Paper {p}", 2015, "full_aff", "Journal Article")
              for p in (130, 131, 132, 401, 411, 421, 430)}
    papers["401"] = ("10.5555/rd.1", "Paper 401", 2015, "full_aff", "Journal Article")
    papers["499"] = ("10.5555/rd.2", "Paper 499", 2015, "full_aff", "Journal Article")
    papers["421"] = ("10.7000/p421", "Paper 421", 2015, "full_other", "Journal Article")
    return _World(
        papers,
        orcid_works=[{"pmid": None, "doi": "10.5555/rd.1"}, {"pmid": None, "doi": "10.5555/rd.2"}],
        s3=["131", "410", "130", "420"], s4=["411", "132", "430"],
        doi_hits={"10.5555/rd.1": ["401"], "10.5555/rd.2": ["402"]},
        served_as={"402": "499", "410": "411", "420": "421"},
    )


async def test_resolve_corpus_equals_todays_when_records_come_back_under_another_pmid(monkeypatch):
    new, old = await _both_resolve(monkeypatch, _redirect_world())
    _assert_same_result(new, old)
    # The fixture exercises what it claims: 402's record (499) is kept, 410 and 411
    # both arrive as 411 and collapse as a duplicate title, 420 arrives as 421, a
    # PMID no stage proposed, so it carries no stages.
    assert {"499", "411", "421"} <= {r["pmid"] for r in old.kept}
    assert old.dropped["duplicate_title"] == 1
    assert [r["stages"] for r in old.kept if r["pmid"] in {"421", "499"}] == [[], []]


async def test_a_two_candidate_batch_verifies_a_record_served_under_another_pmid(monkeypatch):
    """Correction 2: a requested PMID missing from the batch falls back to today's
    single fetch and its ``records[0]``, so the DOI still maps to the ESearch PMID."""
    _install(monkeypatch, _redirect_world())
    dois = ["10.5555/rd.1", "10.5555/rd.2"]
    old = await frozen.convert_dois_to_pmids(dois, strict=True, permanently_dropped=[])
    new = await pubmed.convert_dois_to_pmids(dois, strict=True, permanently_dropped=[])
    assert old == {"10.5555/rd.1": "401", "10.5555/rd.2": "402"}
    assert list(new.items()) == list(old.items())


async def test_a_clean_verification_batch_is_one_request(monkeypatch):
    """The fallback is for the ambiguous batch only: when every PMID comes back
    under its own number, the mixed world verifies its five hits in one EFetch."""
    world = _mixed_world()
    calls: list[str] = []
    inner = world.handler

    def handler(request):
        if "efetch" in str(request.url):
            calls.append(request.url.params["id"])
        return inner(request)

    world.handler = handler
    _install(monkeypatch, world)
    dois = [x["doi"] for x in world.orcid_works + world.openalex_works if x.get("doi") and not x.get("pmid")]
    await pubmed.convert_dois_to_pmids(dois, strict=True)
    assert calls == ["202,205,206,207,208"]


def _failure_world(**overrides) -> _World:
    """Per-item failures on both lookups, interleaved: a verification 400 (vf.1),
    then an ESearch 400 (ef.1), a hit, another verification 400, a dropped DOI whose
    paper arrives through S3 anyway (vf.3), and an S3 PMID whose EFetch 400s."""
    papers = {str(p): (f"10.7000/p{p}", f"Paper {p}", 2000 + p % 5, "full_aff", "Journal Article")
              for p in (140, 141, 142, 143, 502, 506)}
    papers["505"] = ("10.5555/vf.3", "Paper 505", 2001, "full_aff", "Journal Article")
    kwargs = dict(
        orcid_works=[{"pmid": None, "doi": d} for d in
                     ("10.5555/vf.1", "10.5555/ef.1", "10.5555/ok.1", "10.5555/vf.2",
                      "10.5555/vf.3", "10.5555/ok.2")],
        s3=["141", "505", "510", "140"], s4=["142", "143"],
        doi_hits={"10.5555/vf.1": ["501"], "10.5555/ok.1": ["502"], "10.5555/vf.2": ["503"],
                  "10.5555/vf.3": ["504"], "10.5555/ok.2": ["506"]},
        bad_pmids={"501", "503", "504", "510"}, bad_dois={"10.5555/ef.1"},
    )
    papers["502"] = ("10.5555/ok.1", "Paper 502", 2002, "full_aff", "Journal Article")
    papers["506"] = ("10.5555/ok.2", "Paper 506", 2002, "full_aff", "Journal Article")
    kwargs.update(overrides)
    return _World(papers, **kwargs)


async def test_resolve_corpus_equals_todays_with_per_item_failures(monkeypatch):
    new, old = await _both_resolve(monkeypatch, _failure_world())
    _assert_same_result(new, old)
    assert old.permanently_dropped == ["10.5555/vf.1", "10.5555/ef.1", "10.5555/vf.2", "510"]


async def test_the_systemic_run_spans_esearch_and_verification_as_today(monkeypatch):
    """ESearch 400, verification 400, ESearch 400 on consecutive DOIs is three
    identical per-item failures in a row: today's resolver raises at the third."""
    world = _failure_world(
        orcid_works=[{"pmid": None, "doi": d} for d in ("10.5555/ef.1", "10.5555/vf.1", "10.5555/ef.2",
                                                         "10.5555/ok.1")],
        bad_dois={"10.5555/ef.1", "10.5555/ef.2"},
    )
    new, old = await _both_resolve(monkeypatch, world)
    assert isinstance(old, corpus.CorpusStageError)
    assert isinstance(new, corpus.CorpusStageError)
    assert str(new) == str(old)


@pytest.mark.parametrize("world", [_mixed_world, _redirect_world, _failure_world])
@pytest.mark.parametrize("strict", [True, False])
async def test_convert_dois_to_pmids_equals_todays(monkeypatch, world, strict):
    """The mapping (in insertion order) and the drops, strict and non-strict
    (``fetch_abstract`` is the non-strict caller)."""
    w = world()
    _install(monkeypatch, w)
    dois = [x["doi"] for x in w.orcid_works + w.openalex_works if x.get("doi") and not x.get("pmid")]
    results = []
    for convert in (pubmed.convert_dois_to_pmids, frozen.convert_dois_to_pmids):
        dropped: list[str] = []
        results.append((list((await convert(dois, strict=strict, permanently_dropped=dropped)).items()), dropped))
    assert results[0] == results[1]
