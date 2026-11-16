"""The evidence clients and cache (plan Task 4.1, spec 2026-10-30 §6.3, §7.4 step 5, §11.2).
No network: HTTP runs over an httpx.MockTransport on recorded-shape fixture JSON, and Mondo is
installed from small invented release files."""
import json

import httpx
import pytest
from sqlalchemy import select

from src.config import get_settings
from src.landscaping.evidence import (
    cache,
    chembl,
    mondo,
    openfda,
    opentargets,
    orphadata,
    pubtator,
    trials,
)
from src.models.landscape import LandscapeEvidenceCache
from src.services import http_pacing, project_search


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    """Retries and paced starts do not sleep; each pacer starts with an empty schedule."""
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(http_pacing, "_sleep", no_sleep)
    for pacer in (opentargets._PACER, pubtator._PACER, openfda._PACER):
        pacer.reset()
    monkeypatch.setattr(opentargets._PACER, "_interval", 0.0)
    monkeypatch.setattr(openfda._PACER, "_interval", 0.0)


def _client_with(handler):
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- cache -------------------------------------------------------------------------------


async def test_cache_hit_skips_fetch(db_session):
    calls = []

    async def fetch():
        calls.append(1)
        return {"score": 0.5}

    first = await cache.cached(db_session, "opentargets", "26.09", "assoc:ENSG1:MONDO:1", fetch)
    second = await cache.cached(db_session, "opentargets", "26.09", "assoc:ENSG1:MONDO:1", fetch)

    assert first == second == {"score": 0.5}
    assert len(calls) == 1


async def test_newer_release_expires_old_rows(db_session):
    async def fetch():
        return {"v": 1}

    await cache.cached(db_session, "chembl", "ChEMBL_9", "mech:CHEMBL1", fetch)
    await cache.cached(db_session, "chembl", "ChEMBL_9", "mech:CHEMBL2", fetch)
    await cache.cached(db_session, "hgnc", "2026-09", "sym:ABC1", fetch)
    await cache.cached(db_session, "chembl", "ChEMBL_10", "mech:CHEMBL3", fetch)

    rows = (await db_session.execute(
        select(LandscapeEvidenceCache.source, LandscapeEvidenceCache.release,
               LandscapeEvidenceCache.query_key)
        .where(LandscapeEvidenceCache.source.in_(("chembl", "hgnc"))))).all()
    assert sorted(rows) == [("chembl", "ChEMBL_10", "mech:CHEMBL3"),
                            ("hgnc", "2026-09", "sym:ABC1")]



async def test_orphadata_lookups_keyed_by_its_dataset_date(monkeypatch):
    urls = []

    def handler(request):
        urls.append(str(request.url))
        return httpx.Response(200, json={"data": {"__count": 1, "results": {
            "Date": "2099-06-23 07:57:58", "ORPHAcode": 558, "Prevalence": [],
        }}})

    monkeypatch.setattr(orphadata, "_make_client", _client_with(handler))
    monkeypatch.setattr(orphadata._PACER, "_interval", 0.0)
    monkeypatch.setattr(cache, "_release_memo", {})

    assert await cache.current_release("orphadata") == "2099-06-23"
    assert await cache.current_release("orphadata") == "2099-06-23"  # memoised
    assert urls == ["https://api.orphadata.com/rd-epidemiology/orphacodes/558"]

# --- Open Targets -------------------------------------------------------------------------

_OT_TARGET = {"data": {"target": {
    "id": "ENSG00000000001",
    "approvedSymbol": "FAKE1",
    "tractability": [{"label": "Approved Drug", "modality": "SM", "value": False},
                     {"label": "Structure with Ligand", "modality": "SM", "value": True}],
    "isEssential": True,
    "depMapEssentiality": [{"tissueName": "lung",
                            "screens": [{"cellLineName": "LX-1", "geneEffect": -1.2}]}],
    "safetyLiabilities": [{"event": "hepatotoxicity", "eventId": "EFO_1", "datasource": "X"}],
    "geneticConstraint": [{"constraintType": "lof", "oe": 0.1, "oeLower": 0.05,
                           "oeUpper": 0.2, "score": 3.1}],
    "functionDescriptions": ["An invented kinase."],
    "baselineExpression": {"count": 1, "rows": [{
        "tissueBiosample": {"biosampleId": "UBERON_1", "biosampleName": "liver"},
        "median": 12.5, "unit": "TPM", "datasourceId": "gtex", "specificity_score": 0.3}]},
    "subcellularLocations": [{"location": "Nucleus", "source": "uniprot", "termSL": "SL-1"}],
    "chemicalProbes": [{"id": "PROBE1", "control": None, "isHighQuality": True,
                        "mechanismOfAction": ["inhibitor"], "origin": ["experimental"]}],
    "drugAndClinicalCandidates": {"count": 1, "rows": [{
        "id": "abc", "maxClinicalStage": "PHASE_2", "drug": {"id": "CHEMBL9", "name": "FAKEMAB"},
        "diseases": [{"diseaseFromSource": "x", "disease": {"id": "MONDO_0000002",
                                                             "name": "y"}}]}]},
    "homologues": [{"speciesId": "10090", "speciesName": "Mouse",
                    "targetGeneId": "ENSMUSG1", "targetGeneSymbol": "Fake1",
                    "homologyType": "ortholog_one2one", "isHighConfidence": "1"}],
}}}

_OT_ASSOCIATION = {"data": {"disease": {"associatedTargets": {"count": 1, "rows": [{
    "score": 0.42, "datatypeScores": [{"id": "genetic_association", "score": 0.31},
                                      {"id": "literature", "score": 0.6}]}]}}}}


async def test_ot_target_fields_parsed(monkeypatch):
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "associatedTargets" in body["query"]:
            return httpx.Response(200, json=_OT_ASSOCIATION)
        return httpx.Response(200, json=_OT_TARGET)

    monkeypatch.setattr(opentargets, "_make_client", _client_with(handler))
    target = await opentargets.ot_target("ENSG00000000001")
    assoc = await opentargets.ot_association("ENSG00000000001", "MONDO:0000002")

    expected = _OT_TARGET["data"]["target"]
    for name in ("tractability", "isEssential", "depMapEssentiality", "safetyLiabilities",
                 "geneticConstraint", "functionDescriptions", "baselineExpression",
                 "subcellularLocations", "chemicalProbes", "drugAndClinicalCandidates",
                 "homologues"):
        assert name in bodies[0]["query"]
        assert target[name] == expected[name]
    assert bodies[0]["variables"]["id"] == "ENSG00000000001"
    assert assoc["overall"] == 0.42 and assoc["genetic"] == 0.31
    assert bodies[1]["variables"] == {"disease": "MONDO_0000002", "target": "ENSG00000000001"}


# --- Mondo --------------------------------------------------------------------------------

_OBO = """format-version: 1.2
data-version: mondo/releases/2099-01-01/mondo-simple.owl

[Term]
id: MONDO:0000001
name: disease

[Term]
id: MONDO:0000010
name: invented organ disease
synonym: "organopathy" EXACT []
synonym: "organ trouble" BROAD []
is_a: MONDO:0000001 ! disease

[Term]
id: MONDO:0000011
name: invented organ disease type 1
is_a: MONDO:0000010 {source="MONDO:Example"} ! invented organ disease

[Term]
id: MONDO:0000012
name: shared synonym disease
synonym: "organopathy" EXACT []
is_a: MONDO:0000001 ! disease

[Term]
id: MONDO:0000099
name: obsolete invented disease
is_obsolete: true
"""

_SSSOM = """# curie_map:
#   mesh: http://identifiers.org/mesh/
subject_id\tsubject_label\tpredicate_id\tobject_id\tobject_label\tmapping_justification
MONDO:0000010\tinvented organ disease\tskos:exactMatch\tmesh:D000010\t\tsemapv:x
MONDO:0000011\tinvented organ disease type 1\tskos:closeMatch\tmesh:D000011\t\tsemapv:x
MONDO:0000011\tinvented organ disease type 1\tskos:exactMatch\tDOID:11\t\tsemapv:x
"""


async def test_mondo_ancestor(monkeypatch):
    monkeypatch.setattr(mondo, "_data", None)
    with pytest.raises(mondo.MondoNotLoaded):
        mondo.is_ancestor("MONDO:0000001", "MONDO:0000011")
    mondo.install(mondo.build("v2099-01-01", _OBO, _SSSOM))

    assert mondo.is_ancestor("MONDO:0000001", "MONDO:0000011")
    assert mondo.is_ancestor("MONDO:0000010", "MONDO:0000011")
    assert not mondo.is_ancestor("MONDO:0000011", "MONDO:0000010")
    assert not mondo.is_ancestor("MONDO:0000010", "MONDO:0000010")
    assert not mondo.is_ancestor("MONDO:0000012", "MONDO:0000011")
    assert mondo.mondo_parents("MONDO:0000011") == frozenset({"MONDO:0000010"})
    assert mondo.mondo_label_match("Invented  Organ Disease") == "MONDO:0000010"
    assert mondo.mondo_label_match("organopathy") is None  # two terms claim it
    assert mondo.mondo_label_match("organ trouble") is None  # BROAD, not exact
    assert mondo.mondo_label_match("obsolete invented disease") is None
    assert await mondo.mondo_from_mesh("MESH:D000010") == "MONDO:0000010"
    assert await mondo.mondo_from_mesh("D000011") is None  # close match only
    assert await mondo.mondo_release() == "v2099-01-01"


_SSSOM_MORE = _SSSOM + (
    "MONDO:0000010\tinvented organ disease\tskos:exactMatch\tOrphanet:900010\t\tsemapv:x\n"
    "MONDO:0000011\tinvented organ disease type 1\tskos:exactMatch\tOrphanet:900011\t\tsemapv:x\n"
    "MONDO:0000011\tinvented organ disease type 1\tskos:exactMatch\tOrphanet:900012\t\tsemapv:x\n"
    "MONDO:0000012\tshared synonym disease\tskos:closeMatch\tOrphanet:900013\t\tsemapv:x\n"
    "MONDO:0000012\tshared synonym disease\tskos:exactMatch\tmesh:D000012\t\tsemapv:x\n"
    "MONDO:0000012\tshared synonym disease\tskos:exactMatch\tmesh:D000013\t\tsemapv:x\n"
)


def test_mondo_label_orpha_and_mesh_from_release_files(monkeypatch):
    monkeypatch.setattr(mondo, "_data", None)
    with pytest.raises(mondo.MondoNotLoaded):
        mondo.mondo_label("MONDO:0000010")
    mondo.install(mondo.build("v2099-01-01", _OBO, _SSSOM_MORE))

    assert mondo.mondo_label("MONDO:0000010") == "invented organ disease"
    assert mondo.mondo_label("MONDO:0000099") is None  # obsolete
    assert mondo.mondo_label("MONDO:0000404") is None
    assert mondo.mondo_orpha("MONDO:0000010") == "ORPHA:900010"
    assert mondo.mondo_orpha("MONDO:0000011") is None  # two exact mappings
    assert mondo.mondo_orpha("MONDO:0000012") is None  # close match only
    assert mondo.mondo_mesh("MONDO:0000010") == "MESH:D000010"
    assert mondo.mondo_mesh("MONDO:0000011") is None  # close match only
    assert mondo.mondo_mesh("MONDO:0000012") is None  # two exact mappings


# --- ChEMBL -------------------------------------------------------------------------------


def _chembl_target(chembl_id, accession, *, target_type="SINGLE PROTEIN",
                   organism="Homo sapiens"):
    return {"target_chembl_id": chembl_id, "target_type": target_type, "organism": organism,
            "pref_name": "Invented kinase", "tax_id": 9606, "species_group_flag": False,
            "cross_references": [],
            "target_components": [{"accession": accession, "component_type": "PROTEIN"}]}


async def test_chembl_target_for_uniprot_exact_one(monkeypatch):
    by_accession = {
        "Q9X001": [_chembl_target("CHEMBL9900001", "Q9X001")],
        "Q9X002": [_chembl_target("CHEMBL9900002", "Q9X002"),
                   _chembl_target("CHEMBL9900003", "Q9X002")],
        "Q9X004": [_chembl_target("CHEMBL9900004", "Q9X999")],
    }
    params = []

    def handler(request):
        query = dict(request.url.params)
        params.append(query)
        rows = by_accession.get(query["target_components__accession"], [])
        return httpx.Response(200, json={
            "page_meta": {"limit": 1000, "next": None, "offset": 0, "previous": None,
                          "total_count": len(rows)},
            "targets": rows,
        })

    monkeypatch.setattr(chembl, "_make_client", _client_with(handler))
    assert await chembl.chembl_target_for_uniprot("Q9X001") == "CHEMBL9900001"
    assert await chembl.chembl_target_for_uniprot("Q9X002") is None  # two targets
    assert await chembl.chembl_target_for_uniprot("Q9X003") is None  # none
    assert await chembl.chembl_target_for_uniprot("Q9X004") is None  # another component
    assert params[0]["target_type"] == "SINGLE PROTEIN"
    assert params[0]["organism"] == "Homo sapiens"


# --- pacing and budgets -------------------------------------------------------------------


async def test_pacer_rates(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "landscape_pubtator_max_rps", 5.0)
    assert pubtator._PACER._current_interval() == pytest.approx(0.2)
    monkeypatch.setattr(settings, "landscape_pubtator_max_rps", 3.0)
    assert pubtator._PACER._current_interval() == pytest.approx(1 / 3)

    statuses = [429, 200]
    seen = []

    def handler(request):
        seen.append(request.url.params["pmids"])
        status = statuses.pop(0)
        if status == 429:
            return httpx.Response(429)
        return httpx.Response(200, json={"PubTator3": [{"pmid": 11, "passages": [{
            "annotations": [{"infons": {"type": "Gene", "identifier": "1956",
                                        "database": "ncbi_gene", "name": "FAKE1"},
                             "text": "FAKE1"}]}]}]})

    monkeypatch.setattr(pubtator, "_make_client", _client_with(handler))
    monkeypatch.setattr(pubtator._PACER, "_interval", 0.0)
    out = await pubtator.pubtator_annotations(["11", "12"])
    assert seen == ["11,12", "11,12"]  # the 429 was retried
    assert out["11"][0]["identifier"] == "1956" and out["12"] == []
    # the flat shape normalization reads (type, text, identifier, name)
    assert out["11"][0] == {"type": "Gene", "identifier": "1956", "text": "FAKE1",
                            "database": "ncbi_gene", "name": "FAKE1"}

    monkeypatch.setattr(settings, "landscape_openfda_daily_requests", 4)
    openfda._PACER.day, openfda._PACER.used = "", 0
    requests = []

    def fda(request):
        requests.append(request)
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})

    monkeypatch.setattr(openfda, "_make_client", _client_with(fda))
    for _ in range(4):
        assert await openfda.fda_approvals("inventamab") == []
    assert openfda._PACER.used == len(requests) == 4
    assert openfda.MIN_INTERVAL_S >= 60 / 240  # keyless openFDA: 240 requests a minute


async def test_fda_daily_budget_stop(monkeypatch):
    monkeypatch.setattr(get_settings(), "landscape_openfda_daily_requests", 2)
    monkeypatch.setattr(openfda, "_today", lambda: "2099-01-01")
    openfda._PACER.day, openfda._PACER.used = "", 0
    hits = []

    def handler(request):
        hits.append(1)
        return httpx.Response(200, json={"results": [{
            "application_number": "NDA000001", "sponsor_name": "Invented Pharma",
            "openfda": {"brand_name": ["INVENTA"], "generic_name": ["INVENTAMAB"]},
            "products": [{"marketing_status": "Prescription"}],
            "submissions": [{"submission_type": "ORIG", "submission_status": "AP",
                             "submission_status_date": "20200101"}]}]})

    monkeypatch.setattr(openfda, "_make_client", _client_with(handler))
    first = await openfda.fda_approvals("inventamab")
    await openfda.fda_approvals("inventamab")
    with pytest.raises(openfda.DailyBudgetExhausted):
        await openfda.fda_approvals("inventamab")

    assert len(hits) == 2
    assert first[0]["approval_date"] == "20200101"
    monkeypatch.setattr(openfda, "_today", lambda: "2099-01-02")
    await openfda.fda_approvals("inventamab")
    assert len(hits) == 3


async def test_trials_reuses_project_pacer(monkeypatch):
    waits = []

    async def wait():
        waits.append(1)

    monkeypatch.setattr(project_search._TRIALS_PACER, "wait", wait)
    pages = [
        {"totalCount": 3, "nextPageToken": "t2", "studies": [
            {"protocolSection": {"designModule": {"phases": ["PHASE2"]},
                                 "statusModule": {"overallStatus": "RECRUITING"}}},
            {"protocolSection": {"designModule": {"phases": ["PHASE1", "PHASE2"]},
                                 "statusModule": {"overallStatus": "COMPLETED"}}}]},
        {"totalCount": 3, "studies": [
            {"protocolSection": {"statusModule": {"overallStatus": "COMPLETED"}}}]},
    ]
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=pages.pop(0))

    monkeypatch.setattr(trials, "_make_client", _client_with(handler))
    out = await trials.trials_count("invented organ disease", "inventamab")

    assert trials._TRIALS_PACER is project_search._TRIALS_PACER
    assert len(waits) == 2
    assert seen[0]["query.cond"] == "invented organ disease"
    assert seen[1]["pageToken"] == "t2"
    assert out == {"total": 3,
                   "by_phase": {"PHASE2": 1, "PHASE1/PHASE2": 1, trials.NO_PHASE: 1},
                   "by_status": {"RECRUITING": 1, "COMPLETED": 2}}
