"""NCBI E-utilities (PubMed / PMC / ID-converter), live. Task T2.

`src/services/pubmed.py` is the largest external surface in the system and the one
whose failure mode is silent-and-wrong rather than loud: a mis-parsed ArticleId
attributes someone else's paper to a PI (issue #5) and nothing downstream notices.

Rule L1 is why the drift tests exist. `tests/contract/test_pubmed_contract.py` pins the
parser against a HAND-WRITTEN `EFETCH_XML` literal — not a recorded response. If NCBI
renames an element or moves an id, all 10 of those tests still pass and production
breaks. Two things are therefore checked against live data here: the element paths the
fixture claims NCBI emits, and the parsed keys the fixture claims our parser produces.

Rule L3: every assertion message below names which of the four diagnoses it observed —
provider down, rate limited, schema changed, or our parser broken.

Records were chosen for permanence (Rule L2). Only immutable facts are pinned: a
published DOI, a publication year, an assigned PMCID. Titles, abstracts and
availability are asserted for shape only.
"""

import inspect
import re
import xml.etree.ElementTree as ET

import httpx
import pytest
import respx

from src.services import pubmed

pytestmark = [pytest.mark.live_api]

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# Watson & Crick 1953, Nature. Seventy years old, in every textbook, and structurally
# minimal: no abstract, no PMC deposit, no reference list. It is the "thin record" case
# the parser must survive. DOI and year are immutable properties of a published paper.
WATSON_CRICK_PMID = "13054692"
WATSON_CRICK_DOI = "10.1038/171737a0"
WATSON_CRICK_YEAR = 1953

# Jinek et al. 2012, Science (the CRISPR-Cas9 programmable-nuclease paper). Chosen as
# the "rich record": abstract, ELocationID, an NIH author manuscript in PMC, and — the
# reason it is here rather than any other paper — a <ReferenceList> carrying 23 OTHER
# papers' PMCIDs. That is the live trap for the issue-#5 bug.
JINEK_PMID = "22745249"
JINEK_DOI = "10.1126/science.1225829"
JINEK_PMCID = "PMC6286148"
JINEK_YEAR = 2012

# Li et al. 2009, Bioinformatics — "The Sequence Alignment/Map format and SAMtools".
# Fully open access in PMC, and its full text has a section titled "2 METHODS". Note
# that "2 methods" is NOT in `_extract_methods_section`'s exact-title set, so this
# record exercises the substring fallback, which is the tier that actually fires in
# production (see the namespace note in test_extract_methods_uses_the_unnamespaced...).
SAMTOOLS_PMCID = "PMC2723002"

# Bolger et al. 2014, Bioinformatics — "Trimmomatic". Also fully open access with a
# 60 kB body, but its sections are titled ALGORITHMS / IMPLEMENTATION / RESULTS: there
# is no methods-titled section at all. This is the negative control for T2.4 — a real,
# complete, full-text article that must yield None rather than "".
TRIMMOMATIC_PMCID = "PMC4103590"


# --------------------------------------------------------------------------- helpers


def element_paths(root: ET.Element, prefix: str = "") -> set[str]:
    """Every slash-delimited element path in an XML tree.

    Used to compare the hand-written fixture XML's shape against the live response.
    Unlike the ORCID drift walker there is no empty-container problem here (an XML
    element either exists or it does not), but the *record*-level analogue is real:
    a thin record legitimately lacks <Abstract>. That is handled by choosing a rich
    record and by classifying every miss below rather than failing on it blindly.
    """
    here = f"{prefix}/{root.tag}"
    out = {here}
    for child in root:
        out |= element_paths(child, here)
    return out


def methods_titled_sections(xml_text: str) -> list[str]:
    """Titles of every <sec> whose own <title> mentions "method", namespace or not.

    This is an INDEPENDENT probe of the live XML — deliberately not routed through
    `_extract_methods_section` — so a failure can be attributed. If this finds a
    methods section and the service returns None, the service is broken; if this finds
    none, the article genuinely has none and the None is correct.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    found = []
    for tag in ("{http://jats.nlm.nih.gov}sec", "sec"):
        title_tag = "title" if tag == "sec" else "{http://jats.nlm.nih.gov}title"
        for sec in root.findall(f".//{tag}"):
            t = sec.find(title_tag)
            if t is not None and t.text and "method" in t.text.lower():
                found.append(t.text.strip())
    return found


async def _efetch_pubmed_xml(api_budget, pmid: str) -> str:
    api_budget.wait("ncbi")
    resp = await pubmed._ncbi_get(
        f"{EUTILS}/efetch.fcgi",
        {"db": "pubmed", "id": pmid, "rettype": "xml", "retmode": "xml"},
    )
    return resp.text


async def _efetch_pmc_xml(api_budget, pmcid: str) -> str:
    api_budget.wait("ncbi")
    resp = await pubmed._ncbi_get(
        f"{EUTILS}/efetch.fcgi",
        {
            "db": "pmc",
            "id": pmcid.replace("PMC", ""),
            "rettype": "xml",
            "retmode": "xml",
        },
    )
    return resp.text


# ------------------------------------------------------------------------- T2.0


@respx.mock
async def test_ncbi_get_sends_the_required_tool_and_email_parameters():
    """T2.0 — NCBI *requires* `tool=` and `email=` on every E-utilities request.

    Respx-mocked rather than live, but it lives in this tier because the consequence of
    getting it wrong is a live block: NCBI throttles, then blocks by IP, anonymous
    clients that do not identify themselves. A blocked IP breaks the whole tier for
    everyone afterwards, so this runs first.

    Control: an assertion whose passing condition is the *presence* of two parameters is
    worthless if the reader cannot see any parameters at all. So `db` — a parameter the
    caller definitely passes — is asserted first. If `db` is visible and `tool`/`email`
    are not, the omission is real and not an artefact of how the query string is read.
    """
    route = respx.get(f"{EUTILS}/esummary.fcgi").mock(
        return_value=httpx.Response(200, json={"result": {"uids": []}})
    )
    await pubmed._ncbi_get(f"{EUTILS}/esummary.fcgi", {"db": "pubmed", "id": "1"})

    assert route.called, (
        "respx recorded no request — _ncbi_get did not reach the URL under test, so "
        "nothing below is meaningful (EUTILS_BASE may have moved)"
    )
    params = route.calls.last.request.url.params
    assert params.get("db") == "pubmed", (
        "the caller's own parameters are not visible in the recorded query string, so "
        f"this test cannot observe what _ncbi_get sends. Saw: {dict(params)}"
    )

    absent = [k for k in ("tool", "email") if not params.get(k)]
    assert not absent, (
        f"_ncbi_get omits {absent} from every NCBI request. This is not a schema change "
        "and not a network problem — src/services/pubmed.py never adds them. NCBI's "
        "E-utilities usage policy requires both on every request and throttles, then "
        "blocks by IP, clients that omit them; the whole profile pipeline runs through "
        f"this one function. Saw: {dict(params)}"
    )


# ------------------------------------------------------------------------- T2.1


async def test_two_stable_pubmed_records_fetch_and_parse(api_budget):
    """T2.1 — shape for everything, exact values only for the immutable ones.

    Both records are requested in a SINGLE efetch call, which makes the control free:
    a parser that ignored its input, returned a constant, or kept only the last article
    in the set would not produce two distinct records with their own correct DOIs.
    """
    api_budget.wait("ncbi")
    recs = await pubmed.fetch_pubmed_records([WATSON_CRICK_PMID, JINEK_PMID])

    assert len(recs) == 2, (
        f"efetch returned {len(recs)} parsed records for 2 PMIDs. Either NCBI is "
        "degraded/rate-limiting (an empty list means _fetch_pubmed_batch swallowed an "
        "HTTP error — see the logged 'Failed to fetch PubMed batch'), or the "
        "PubmedArticle element the parser iterates on has been renamed. "
        f"Got: {[r.get('pmid') for r in recs]}"
    )
    by_pmid = {r["pmid"]: r for r in recs}
    assert set(by_pmid) == {WATSON_CRICK_PMID, JINEK_PMID}, (
        "the parsed PMIDs are not the ones requested — the parser is reading a PMID "
        f"from the wrong element. Got {sorted(by_pmid)}"
    )

    for pmid, rec in by_pmid.items():
        assert isinstance(rec.get("title"), str) and rec["title"].strip(), (
            f"PMID {pmid} parsed with an empty title; ArticleTitle is present on every "
            "PubMed record, so this is a parser or schema problem, not missing data"
        )
        assert isinstance(rec.get("journal"), str) and rec["journal"].strip(), (
            f"PMID {pmid} parsed with no journal — Journal/Title may have moved"
        )
        assert isinstance(rec.get("pub_types"), list) and rec["pub_types"], (
            f"PMID {pmid} parsed with no PublicationType"
        )
        assert isinstance(rec.get("author_count"), int) and rec["author_count"] > 0, (
            f"PMID {pmid} parsed with {rec.get('author_count')} authors — the Author "
            "element may have moved"
        )

    # Immutable: a published DOI and a publication year never change (Rule L2).
    assert by_pmid[WATSON_CRICK_PMID]["doi"] == WATSON_CRICK_DOI
    assert by_pmid[WATSON_CRICK_PMID]["year"] == WATSON_CRICK_YEAR
    assert by_pmid[JINEK_PMID]["doi"] == JINEK_DOI
    assert by_pmid[JINEK_PMID]["year"] == JINEK_YEAR
    assert by_pmid[JINEK_PMID]["pmcid"] == JINEK_PMCID

    # The rich record carries an abstract; the 1953 one does not. Both are correct, and
    # asserting the difference is what proves the abstract path is really being read
    # rather than filled with a constant.
    assert by_pmid[JINEK_PMID]["abstract"].strip(), (
        "the 2012 record parsed with an empty abstract — AbstractText may have moved"
    )
    assert by_pmid[WATSON_CRICK_PMID]["abstract"] == "", (
        "the 1953 record has no <Abstract> in PubMed, so the parser must yield '' for "
        "it. A non-empty value here means abstract text is leaking in from elsewhere: "
        f"{by_pmid[WATSON_CRICK_PMID]['abstract'][:120]!r}"
    )


# ------------------------------------------------------------------------- T2.2


async def test_the_contract_fixture_still_matches_the_real_efetch_xml(api_budget):
    """T2.2, part 1 — Rule L1 for the XML the fixture claims NCBI returns.

    `EFETCH_XML` in tests/contract/test_pubmed_contract.py is a literal somebody typed.
    Every element path it uses is a belief about NCBI's schema that nothing has ever
    checked. This walks those paths and requires them in a live response.

    Control: the live XML must be non-trivial (>1000 chars) and must yield a large path
    set first. If efetch returned an error stub, `missing` would be everything (loud) —
    but if the walker itself broke, `missing` would be empty and the test would pass
    while proving nothing. Both are guarded.
    """
    from tests.contract import test_pubmed_contract as fixture_mod

    live_xml = await _efetch_pubmed_xml(api_budget, JINEK_PMID)
    assert len(live_xml) > 1000, (
        f"efetch returned only {len(live_xml)} chars. That is an error stub or a "
        "rate-limit page, not a record — the comparison below would be vacuous. "
        f"Body starts: {live_xml[:200]!r}"
    )

    fixture_paths = element_paths(ET.fromstring(fixture_mod.EFETCH_XML))
    assert len(fixture_paths) >= 15, (
        f"the fixture walker found only {len(fixture_paths)} element paths — it is "
        "broken, so the comparison below would be meaningless"
    )
    live_paths = element_paths(ET.fromstring(live_xml))
    assert len(live_paths) >= 25, (
        f"the live record has only {len(live_paths)} element paths — NCBI returned "
        "something unexpected and the comparison below would be meaningless"
    )

    missing = sorted(p for p in fixture_paths if p not in live_paths)
    assert not missing, (
        "NCBI's live efetch response no longer contains element paths that the "
        "hand-written EFETCH_XML fixture in tests/contract/test_pubmed_contract.py "
        "asserts on. This is a SCHEMA CHANGE, not a network or parser fault: those "
        "contract tests are pinning a document shape that no longer exists.\n  "
        + "\n  ".join(missing)
    )

    # The parser branches on two attributes, not just on element names. A rename here
    # is invisible to the path comparison above and silently empties every DOI/PMCID.
    live_root = ET.fromstring(live_xml)
    assert any(
        el.get("IdType") for el in live_root.findall(".//ArticleId")
    ), "ArticleId no longer carries an IdType attribute — every DOI and PMCID would be dropped"
    assert any(
        el.get("EIdType") for el in live_root.findall(".//ELocationID")
    ), "ELocationID no longer carries an EIdType attribute — the DOI fallback is dead"


async def test_the_parser_produces_the_same_keys_on_live_xml_as_on_the_fixture(api_budget):
    """T2.2, part 2 — Rule L1 for the parser's OUTPUT contract.

    The contract tests assert on nine keys of `_parse_pubmed_xml`'s output. Whether the
    real parser still produces those nine keys from a real document has never been
    checked. Runs the real parser over real XML and compares key sets.

    Control (the T1 empty-container lesson): a key can be absent for two entirely
    different reasons — NCBI changed the document, or *this record* simply has no such
    datum. Each absent key is therefore classified against an INDEPENDENT XPath probe of
    the same live XML. "The probe found the data and the parser dropped it" is a parser
    bug; "the probe found nothing either" is unverifiable, not a failure. A minimum
    verified count then stops an all-unverifiable run reporting a pass.

    The baseline is read out of the contract test's SOURCE, not out of the parser's
    output for the fixture. Measured: deriving it from `_parse_pubmed_xml(EFETCH_XML)`
    makes the whole test vacuous, because a parser mutated to drop a key drops it from
    both sides of the comparison and the drift check passes. (A mutant that deleted the
    `pmcid` key survived this test until the baseline was moved off the parser.)
    """
    from tests.contract import test_pubmed_contract as fixture_mod

    contract_test = fixture_mod.test_fetch_pubmed_records_parses_article_scoped_fields
    fixture_keys = set(re.findall(r'\br\["(\w+)"\]', inspect.getsource(contract_test)))
    assert len(fixture_keys) >= 8, (
        f"only {sorted(fixture_keys)} could be read out of the contract test's source. "
        "The scraper is broken (or the test was rewritten), so the comparison below "
        "would be vacuous"
    )

    live_xml = await _efetch_pubmed_xml(api_budget, JINEK_PMID)
    assert len(live_xml) > 1000, (
        f"efetch returned {len(live_xml)} chars — an error stub or rate-limit page, "
        "so nothing below would be meaningful"
    )

    fixture_recs = pubmed._parse_pubmed_xml(fixture_mod.EFETCH_XML)
    assert len(fixture_recs) == 1, (
        "the parser no longer parses its own contract fixture — the drift comparison "
        "has no baseline"
    )
    missing_on_fixture = sorted(fixture_keys - set(fixture_recs[0]))
    assert not missing_on_fixture, (
        "OUR PARSER no longer produces keys that tests/contract/test_pubmed_contract.py "
        f"asserts on, even for that file's own hand-written XML: {missing_on_fixture}. "
        "This is a parser regression, visible without any network access"
    )

    live_recs = pubmed._parse_pubmed_xml(live_xml)
    assert len(live_recs) == 1, (
        f"the real parser produced {len(live_recs)} records from a real single-article "
        "efetch response. OUR PARSER (or NCBI's PubmedArticle element) is broken"
    )
    live_keys = set(live_recs[0])

    # Independent evidence that each datum exists in the live document, found without
    # going through the parser under test.
    root = ET.fromstring(live_xml)
    probes = {
        "pmid": lambda: root.find(".//PMID") is not None,
        "title": lambda: root.find(".//ArticleTitle") is not None,
        "abstract": lambda: root.find(".//AbstractText") is not None,
        "journal": lambda: root.find(".//Journal/Title") is not None,
        "year": lambda: root.find(".//PubDate/Year") is not None,
        "pub_types": lambda: root.find(".//PublicationType") is not None,
        "author_count": lambda: root.find(".//Author") is not None,
        "doi": lambda: any(
            e.get("IdType") == "doi" for e in root.findall(".//ArticleId")
        )
        or any(e.get("EIdType") == "doi" for e in root.findall(".//ELocationID")),
        "pmcid": lambda: any(
            e.get("IdType") == "pmc" for e in root.findall(".//ArticleId")
        ),
    }
    unknown = sorted(k for k in fixture_keys if k not in probes)
    assert not unknown, (
        f"the contract fixture now produces keys this drift test has no probe for: "
        f"{unknown}. Add a probe — until then the comparison silently skips them"
    )

    absent = [k for k in fixture_keys if k not in live_keys]
    parser_dropped = sorted(k for k in absent if probes[k]())
    not_in_this_record = sorted(k for k in absent if not probes[k]())

    assert not parser_dropped, (
        "OUR PARSER is broken (not NCBI): for these keys the datum is demonstrably "
        "present in the live XML — an independent XPath probe found it — and "
        f"_parse_pubmed_xml did not emit the key: {parser_dropped}. "
        f"Parsed keys were {sorted(live_keys)}"
    )
    verified = fixture_keys - set(not_in_this_record)
    assert len(verified) >= 8, (
        f"only {len(verified)} of {len(fixture_keys)} fixture keys could be checked "
        f"against live data ({not_in_this_record} are absent from this record too). "
        "Pick a richer PMID — this run proved almost nothing"
    )


async def test_the_parser_ignores_reference_list_article_ids_on_a_live_record(api_budget):
    """The issue-#5 regression, against live data.

    A recursive `.//ArticleId` search also matches the <ReferenceList>, whose ids belong
    to *cited* papers; the old code kept the last match and stamped publications with a
    reference's DOI or PMCID. Contract tests cannot catch a regression here because the
    hand-written fixture has no reference list — this is the only test in the system
    that runs the parser over a document where the trap is actually set.

    Control: the trap must be armed. The test asserts the live record really does carry
    other papers' PMCIDs before asserting that the parser picked the article's own; if
    NCBI stops shipping reference lists, this reports "inconclusive", not "pass".
    """
    live_xml = await _efetch_pubmed_xml(api_budget, JINEK_PMID)
    root = ET.fromstring(live_xml)

    own_container = root.find(".//PubmedArticle/PubmedData/ArticleIdList")
    assert own_container is not None, (
        "PubmedData/ArticleIdList is gone from the live response — the parser reads "
        "this exact path, so every DOI and PMCID would silently disappear"
    )
    own_pmcids = {
        e.text for e in own_container.findall("ArticleId") if e.get("IdType") == "pmc"
    }
    all_pmcids = {
        e.text for e in root.findall(".//ArticleId") if e.get("IdType") == "pmc"
    }
    foreign = all_pmcids - own_pmcids
    assert len(foreign) >= 2, (
        f"this record now carries only {len(foreign)} reference-scoped PMCIDs, so the "
        "trap this test exists to spring is no longer set and the assertion below "
        "would pass for a parser that reads the reference list. Pick a PMID whose "
        "PubMed record still has a <ReferenceList>"
    )

    recs = pubmed._parse_pubmed_xml(live_xml)
    assert len(recs) == 1
    assert recs[0]["pmcid"] == JINEK_PMCID, (
        "OUR PARSER attributed the wrong PMCID to the article. A PMCID is immutable "
        f"once assigned, so {recs[0].get('pmcid')!r} is not a data change — if it is "
        f"one of {sorted(foreign)[:3]} the reference-list scoping fix has regressed"
    )
    assert recs[0]["doi"] == JINEK_DOI, (
        f"OUR PARSER attributed DOI {recs[0].get('doi')!r} to a paper whose published "
        f"(immutable) DOI is {JINEK_DOI} — the same reference-list regression"
    )


# ------------------------------------------------------------------------- T2.3


async def test_id_conversion_round_trips_and_a_nonsense_doi_maps_to_nothing(api_budget):
    """T2.3 — PMID → authoritative DOI → PMID → PMCID, asserted as a round trip.

    Nothing here is pinned to a value NCBI could legitimately change except the PMCID,
    which is immutable once assigned. The round trip is self-checking: whatever DOI
    PubMed reports for this PMID must resolve back to the same PMID.

    Control: a syntactically valid but nonexistent DOI must map to NOTHING. Mapping it
    to *some* PMID is the failure that silently attributes a stranger's paper to a PI,
    and without this leg "the converter returned a mapping" is satisfied by a converter
    that returns a mapping for anything.
    """
    api_budget.wait("ncbi")
    auth = await pubmed.fetch_authoritative_dois([JINEK_PMID])
    assert JINEK_PMID in auth, (
        "esummary returned no DOI for a PMID that has one. Either NCBI is degraded "
        "(fetch_authoritative_dois swallows the error and returns {}), or the "
        f"articleids/idtype JSON shape changed. Got: {auth}"
    )
    assert auth[JINEK_PMID].lower() == JINEK_DOI.lower(), (
        f"esummary reports {auth[JINEK_PMID]!r} as the DOI for PMID {JINEK_PMID}, but "
        f"the published (immutable) DOI is {JINEK_DOI}. Either the esummary parser is "
        "reading the wrong articleids entry, or the value now carries a URL prefix"
    )

    api_budget.wait("ncbi")
    api_budget.wait("ncbi")  # idconv, plus a possible esearch fallback
    back = await pubmed.convert_dois_to_pmids([auth[JINEK_PMID]])
    assert back.get(auth[JINEK_PMID]) == JINEK_PMID, (
        f"the DOI PubMed itself reports for PMID {JINEK_PMID} does not round-trip back "
        f"to it. Got {back!r} — the ID converter's record shape (doi/pmid keys) or the "
        "esearch fallback's idlist path has changed"
    )

    api_budget.wait("ncbi")
    pmcids = await pubmed.convert_pmids_to_pmcids([JINEK_PMID])
    assert pmcids.get(JINEK_PMID) == JINEK_PMCID, (
        f"PMID {JINEK_PMID} no longer maps to {JINEK_PMCID}. A PMCID is immutable once "
        f"assigned, so this is the converter, not the data. Got: {pmcids!r}"
    )

    # Control leg. Well-formed, registrant prefix 10.9999 is not issued to anyone.
    bogus = "10.9999/copi-live-test-no-such-doi-2f4a1c"
    api_budget.wait("ncbi")
    api_budget.wait("ncbi")  # idconv, then the esearch fallback for the unresolved DOI
    nothing = await pubmed.convert_dois_to_pmids([bogus])
    assert nothing == {}, (
        f"a nonexistent DOI resolved to {nothing!r}. This is the failure that attributes "
        "someone else's paper to a PI: either the ID converter's error-record check "
        "(status == 'error') no longer matches, or the esearch fallback is returning "
        "unrelated hits for a term that matches nothing"
    )


# ------------------------------------------------------------------------- T2.4


async def test_pmc_methods_extraction_on_real_open_access_articles(api_budget):
    """T2.4 — `fetch_pmc_methods` / `_extract_methods_section` against live PMC.

    Three legs, because "returns a methods section" and "returns None" are only
    meaningful together:

      1. an OA article WITH a methods section  -> a non-empty string
      2. an OA article WITHOUT one             -> None (not "", which the caller
                                                  cannot distinguish from "empty")
      3. a PMC record with no full text at all -> None, swallowed, not an exception

    Each leg is attributed against an independent probe of the same XML, so a failure
    says whether PMC changed the article or our extractor broke.
    """
    # --- leg 1: has a methods section -------------------------------------------
    raw = await _efetch_pmc_xml(api_budget, SAMTOOLS_PMCID)
    assert len(raw) > 1000, (
        f"PMC returned {len(raw)} chars for {SAMTOOLS_PMCID} — an error stub or a "
        f"metadata-only record, not full text. Body starts: {raw[:200]!r}"
    )
    titled = methods_titled_sections(raw)
    assert titled, (
        f"{SAMTOOLS_PMCID}'s live full text no longer has any section titled with "
        "'method'. That is a change in the TEST DATA, not a bug in the extractor — "
        "pick another open-access article, because the leg below cannot pass"
    )

    methods = pubmed._extract_methods_section(raw)
    assert isinstance(methods, str) and methods.strip(), (
        f"OUR EXTRACTOR returned {methods!r} even though an independent probe found "
        f"methods-titled sections {titled} in the same XML. _extract_methods_section "
        "is broken — most likely its <sec>/<title> traversal or its namespace handling"
    )
    assert len(methods) > 200, (
        f"the extracted methods section is only {len(methods)} chars — the traversal "
        f"is returning a title rather than the section body: {methods[:120]!r}"
    )
    vocabulary = ("align", "sequence", "format", "algorithm", "index", "data")
    hits = [w for w in vocabulary if w in methods.lower()]
    assert hits, (
        "the extracted text contains none of the expected methodological vocabulary "
        f"{vocabulary}, so the extractor probably grabbed the wrong section: "
        f"{methods[:200]!r}"
    )

    # The public wrapper must agree with the extractor (it also strips the PMC prefix,
    # which is the only transformation between them).
    api_budget.wait("ncbi")
    via_service = await pubmed.fetch_pmc_methods(SAMTOOLS_PMCID)
    assert via_service is not None and via_service[:200] == methods[:200], (
        "fetch_pmc_methods disagrees with _extract_methods_section on the same article "
        "— the wrapper's PMC-prefix stripping or its error swallowing is at fault, not "
        f"the parser. Wrapper gave: {(via_service or '')[:120]!r}"
    )

    # --- leg 2: full text, but genuinely no methods section ----------------------
    raw_none = await _efetch_pmc_xml(api_budget, TRIMMOMATIC_PMCID)
    assert len(raw_none) > 1000, (
        f"PMC returned {len(raw_none)} chars for {TRIMMOMATIC_PMCID}; this leg needs a "
        "real full-text body, otherwise 'no methods section' is indistinguishable from "
        "'no article'"
    )
    assert not methods_titled_sections(raw_none), (
        f"{TRIMMOMATIC_PMCID} now HAS a methods-titled section, so it is no longer a "
        "valid negative control. Change the TEST DATA, not the extractor"
    )
    assert pubmed._extract_methods_section(raw_none) is None, (
        "OUR EXTRACTOR invented a methods section for an article that has none. The "
        "caller distinguishes None from '' — returning either a string or '' here "
        f"corrupts that: {pubmed._extract_methods_section(raw_none)!r}"
    )

    # --- leg 3: in PMC, but no full text deposited -------------------------------
    api_budget.wait("ncbi")
    absent = await pubmed.fetch_pmc_methods(JINEK_PMCID)
    assert absent is None, (
        f"{JINEK_PMCID} is a metadata-only PMC record (no <body>), so fetch_pmc_methods "
        f"must return None. Got {type(absent).__name__} {str(absent)[:120]!r} — if this "
        "is '' the caller can no longer tell 'no full text' from 'empty methods'"
    )


async def test_extract_methods_uses_the_unnamespaced_fallback_on_real_pmc_xml(api_budget):
    """PMC's efetch output carries NO JATS namespace, so the first two tiers of
    `_extract_methods_section` — both of which query `{http://jats.nlm.nih.gov}sec` —
    never match a live response. Everything is done by the third, unnamespaced tier,
    whose match is a loose `"method" in title` substring rather than the curated
    exact-title set above it.

    This is asserted rather than left implicit because it means the exact-title
    `methods_keywords` set is dead code against efetch, and anyone tightening the
    substring tier would silently break every extraction. Control: the same document is
    shown to contain sections in the unnamespaced form, so "the namespaced query found
    nothing" cannot be explained by the document being empty.
    """
    raw = await _efetch_pmc_xml(api_budget, SAMTOOLS_PMCID)
    root = ET.fromstring(raw)
    plain = root.findall(".//sec")
    namespaced = root.findall(".//{http://jats.nlm.nih.gov}sec")

    assert plain, (
        "the live PMC document has no <sec> elements at all, so neither branch could "
        "match and this test proves nothing — PMC's full-text shape has changed"
    )
    assert not namespaced, (
        "PMC efetch now DOES emit JATS-namespaced <sec> elements. That is good news, "
        "but it means _extract_methods_section's first two (exact-title) tiers have "
        "started firing for the first time and their behaviour is now live — "
        f"{len(namespaced)} namespaced sections found"
    )


# ------------------------------------------------------------------------- T2.5


async def test_batching_covers_every_pmid_and_makes_the_expected_number_of_calls(
    api_budget, monkeypatch
):
    """T2.5 — `fetch_pubmed_records` chunks at 100; feed it more than one chunk.

    The ids come from a live esearch rather than a hard-coded list, so the test cannot
    go stale and every id is guaranteed to exist right now (Rule L2).

    Two assertions with different targets: the CALL COUNT catches a batch size that
    silently changed (one call for 120 ids would be an over-long URL NCBI rejects; 120
    calls would be a rate-limit ban), and the COVERAGE catches records dropped in the
    middle of a multi-batch merge. Control for the call count: a single-chunk request
    is also measured, so "calls == chunks" cannot be satisfied by a constant.
    """
    want = 120
    api_budget.wait("ncbi")
    resp = await pubmed._ncbi_get(
        f"{EUTILS}/esearch.fcgi",
        {
            "db": "pubmed",
            "term": 'crispr[tiab] AND 2018[dp] AND "journal article"[pt]',
            "retmax": str(want),
            "retmode": "json",
        },
    )
    pmids = resp.json().get("esearchresult", {}).get("idlist", [])
    assert len(pmids) == want, (
        f"esearch returned {len(pmids)} ids for retmax={want}; this test needs more "
        "than one batch's worth or the batching maths below is untested"
    )

    original = pubmed._ncbi_get
    calls: list[dict] = []

    async def counting_ncbi_get(url, params):
        calls.append(dict(params))
        api_budget.wait("ncbi")
        return await original(url, params)

    monkeypatch.setattr(pubmed, "_ncbi_get", counting_ncbi_get)

    recs = await pubmed.fetch_pubmed_records(pmids)
    assert len(calls) == 2, (
        f"{want} PMIDs produced {len(calls)} efetch calls; the code chunks at 100, so "
        "2 is the only correct answer. 1 means the chunk size grew (NCBI rejects "
        f"over-long id lists); {want} means it collapsed to one-per-id and this test "
        "just spent 120 requests against a 3/s limit"
    )
    sent = [len(p["id"].split(",")) for p in calls]
    assert sent == [100, 20], f"batch sizes were {sent}, expected [100, 20]"

    returned = [r.get("pmid") for r in recs]
    assert len(returned) == len(set(returned)), (
        "fetch_pubmed_records returned duplicate PMIDs — the multi-batch merge is "
        "extending the result list with an earlier batch"
    )
    foreign = sorted(set(returned) - set(pmids))
    assert not foreign, (
        f"records came back for PMIDs that were never requested: {foreign[:5]}. The "
        "parser is reading a PMID from the wrong element (a CommentsCorrections or "
        "reference entry), which is how a stranger's paper ends up on a PI's profile"
    )
    dropped = sorted(set(pmids) - set(returned))
    assert not dropped, (
        f"{len(dropped)} of {want} requested PMIDs produced no parsed record: "
        f"{dropped[:5]}. If the second batch is entirely missing, a batch's HTTP error "
        "was swallowed by fetch_pubmed_records (check the log for 'Failed to fetch "
        "PubMed batch' — that would be NCBI rate-limiting, not a parser fault); a "
        "scattered few means those records are not PubmedArticle elements"
    )


# ------------------------------------------------------------------------- T2.6


async def test_reconcile_pub_doi_separates_a_real_match_from_a_near_miss(api_budget):
    """T2.6 — the gate that decides whether a paper is really this PI's.

    The authoritative DOI is taken live from esummary rather than hard-coded, so the
    test exercises whatever format NCBI ships today: if esummary started returning
    `https://doi.org/...` or a lower-cased variant, the "ok" leg below would fail and
    every reconciled publication in production would be marked "corrected".

    Both directions are asserted in the same test. Without the "ok" leg, a function
    that always answered "corrected" would pass; without the mismatch legs, one that
    always answered "ok" would.
    """
    api_budget.wait("ncbi")
    auth_map = await pubmed.fetch_authoritative_dois([JINEK_PMID, WATSON_CRICK_PMID])
    for pmid in (JINEK_PMID, WATSON_CRICK_PMID):
        assert pmid in auth_map, (
            f"esummary gave no authoritative DOI for PMID {pmid}; either NCBI is "
            "degraded (the error is swallowed and {} returned) or articleids changed. "
            f"Got {auth_map!r}"
        )
    auth = auth_map[JINEK_PMID]
    other = auth_map[WATSON_CRICK_PMID]
    assert auth.lower() != other.lower(), "the two control DOIs must differ"

    # Permitted leg — an exact match. If this does not fire, every leg below is
    # satisfied by a function that never matches anything.
    assert pubmed.reconcile_pub_doi(auth, auth) == (auth, "ok"), (
        f"a DOI did not match itself. NCBI's esummary now reports {auth!r}, which "
        "normalize_doi is not canonicalising to the stored form — in production every "
        "correctly-attributed publication would be rewritten as 'corrected'"
    )

    # Same DOI, arriving in the two formats the ingest actually sees.
    assert pubmed.reconcile_pub_doi(f"doi: {auth}", auth) == (auth, "ok"), (
        "a 'doi:'-prefixed DOI is no longer canonicalised to a match — normalize_doi's "
        "prefix stripping regressed"
    )
    assert pubmed.reconcile_pub_doi(f"https://doi.org/{auth}", auth) == (auth, "ok"), (
        "a doi.org URL is no longer canonicalised to a match — normalize_doi's URL "
        "stripping regressed"
    )
    assert pubmed.reconcile_pub_doi(auth.upper(), auth) == (auth.upper(), "ok"), (
        "DOIs are case-insensitive; an upper-cased assigned DOI must still match, and "
        "the STORED form must be the one returned"
    )

    # Denied leg 1 — a whole different real paper's DOI on this PMID. This is exactly
    # the issue-#5 corruption, and the gate must overwrite it with the authoritative one.
    assert pubmed.reconcile_pub_doi(other, auth) == (auth, "corrected"), (
        f"the gate accepted {other!r} (a different, real paper) as the DOI for PMID "
        f"{JINEK_PMID}, whose authoritative DOI is {auth!r}. This is the check that "
        "stops someone else's paper being credited to a PI"
    )

    # Denied leg 2 — a near-miss: the same DOI with one character changed. Catches a
    # comparison loosened to a prefix/substring match.
    near = auth[:-1] + ("9" if auth[-1] != "9" else "8")
    assert near.lower() != auth.lower()
    assert pubmed.reconcile_pub_doi(near, auth) == (auth, "corrected"), (
        f"a one-character-off DOI ({near!r} vs {auth!r}) was accepted as a match — the "
        "comparison has been loosened from equality to something fuzzier"
    )

    # The two remaining documented outcomes, so the action vocabulary is pinned whole.
    assert pubmed.reconcile_pub_doi(None, auth) == (auth, "filled")
    assert pubmed.reconcile_pub_doi(auth, None) == (auth, "unverified")
    assert pubmed.reconcile_pub_doi(None, None) == (None, "none")

    # Sanity on the live value itself: a DOI is "10.<registrant>/<suffix>".
    assert re.match(r"^10\.\d{4,9}/\S+$", auth), (
        f"esummary's authoritative DOI {auth!r} is not in DOI syntax — normalize_doi "
        "is leaving a prefix on, or esummary changed its value format"
    )
